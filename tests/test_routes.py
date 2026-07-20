# -*- coding: utf-8 -*-
"""Route handlers: config GET/save (masking + grouping) and instances/test,
against the stubbed flask.request from conftest.py."""


from routes import api as routes_api
from routes import config_store


def _instance(id_='truenas-test', client_id='idkmanager'):
    return {
        'id': id_, 'name': 'TrueNAS Test Instance', 'client_id': client_id,
        'host': '192.0.2.64', 'port': 8443, 'use_tls': True, 'verify_tls': False,
        'api_key_ro': 'real-secret-ro', 'api_key_rw': None, 'readonly': True,
    }


def test_config_handler_masks_keys_and_groups_by_client(plugin, tmp_plugin_dir, monkeypatch):
    config_store.save_config(routes_api.CONFIG_PATH,
                              {'instances': [_instance()], 'poll': config_store.DEFAULT_POLL})
    resp = routes_api.config_handler()
    _, payload = resp
    assert payload['instances'][0]['api_key_ro'] == '***'
    assert payload['instances_by_client'][0]['client_id'] == 'idkmanager'


def test_config_save_handler_round_trips_masked_key(plugin, tmp_plugin_dir, monkeypatch):
    config_store.save_config(routes_api.CONFIG_PATH,
                              {'instances': [_instance()], 'poll': config_store.DEFAULT_POLL})
    incoming = dict(_instance())
    incoming['api_key_ro'] = '***'
    monkeypatch.setattr(routes_api.request, 'get_json',
                         lambda silent=False: {'instances': [incoming], 'poll': {}})
    routes_api.config_save_handler()
    saved = config_store.load_config(routes_api.CONFIG_PATH)
    assert saved['instances'][0]['api_key_ro'] == 'real-secret-ro'


def test_config_save_handler_rejects_invalid_instance(plugin, tmp_plugin_dir, monkeypatch):
    bad = _instance()
    bad['host'] = ''
    monkeypatch.setattr(routes_api.request, 'get_json',
                         lambda silent=False: {'instances': [bad], 'poll': {}})
    resp, status = routes_api.config_save_handler()
    assert status == 400


def test_instances_test_handler_requires_host_and_key(plugin, tmp_plugin_dir, monkeypatch):
    monkeypatch.setattr(routes_api.request, 'get_json',
                         lambda silent=False: {'id': '', 'host': '', 'port': 443})
    resp, status = routes_api.instances_test_handler()
    assert status == 400


def test_instances_test_handler_uses_stored_key_when_masked(plugin, tmp_plugin_dir, monkeypatch):
    config_store.save_config(routes_api.CONFIG_PATH,
                              {'instances': [_instance()], 'poll': config_store.DEFAULT_POLL})

    captured = {}

    def fake_test_connection(instance_cfg, api_key):
        captured['instance_cfg'] = instance_cfg
        captured['api_key'] = api_key
        return {'ok': True, 'error': None}

    monkeypatch.setattr(routes_api.conn_manager, 'test_connection', fake_test_connection)
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'id': 'truenas-test', 'host': '192.0.2.64', 'port': 8443,
        'use_tls': True, 'verify_tls': False, 'api_key_ro': '***',
    })
    _, payload = routes_api.instances_test_handler()
    assert payload['ok'] is True
    assert captured['api_key'] == 'real-secret-ro'


def test_instances_test_handler_never_persists_config(plugin, tmp_plugin_dir, monkeypatch):
    before = config_store.load_config(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.conn_manager, 'test_connection',
                         lambda instance_cfg, api_key: {'ok': True, 'error': None})
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'id': 'draft', 'host': '1.2.3.4', 'port': 443, 'api_key_ro': 'draft-key',
    })
    routes_api.instances_test_handler()
    after = config_store.load_config(routes_api.CONFIG_PATH)
    assert before == after


# ---------------------------------------------------------------------------
# Regression: instances_test_handler must reject an API key over plain
# ws:// with the SAME guard config_store.validate_instances already applies
# on save — otherwise an operator can untick "use_tls" on the draft form and
# hit "Probar conexión" BEFORE saving, revoking their own production key
# with one click (finding #7).
# ---------------------------------------------------------------------------

def test_instances_test_handler_rejects_api_key_over_plain_ws(plugin, tmp_plugin_dir, monkeypatch):
    called = {'n': 0}

    def fake_test_connection(instance_cfg, api_key):
        called['n'] += 1
        return {'ok': True, 'error': None}

    monkeypatch.setattr(routes_api.conn_manager, 'test_connection', fake_test_connection)
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'id': 'draft', 'host': '192.0.2.64', 'port': 8443,
        'use_tls': False, 'api_key_ro': 'a-real-production-key',
    })
    resp, status = routes_api.instances_test_handler()
    assert status == 400
    _, payload = resp
    assert payload['ok'] is False
    assert 'use_tls' in payload['error']
    # The actual TrueNAS interaction must never have been attempted — the
    # whole point is to never let the key travel over ws:// in the first
    # place, not to report the failure after the fact.
    assert called['n'] == 0


def test_instances_test_handler_allows_plain_ws_without_a_key(plugin, tmp_plugin_dir, monkeypatch):
    """The use_tls guard is specifically about protecting a real API key —
    it must not block a request that simply has no key to protect (which
    already 400s earlier for a different reason: 'host and api_key_ro are
    required')."""
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'id': 'draft', 'host': '192.0.2.64', 'port': 8443, 'use_tls': False,
    })
    resp, status = routes_api.instances_test_handler()
    assert status == 400
    _, payload = resp
    assert 'api_key_ro' in payload['error']


# ---------------------------------------------------------------------------
# Minor hardening: malformed JSON body must produce an accurate 400, not a
# misleading validation error; a disk failure on save must 500 with
# context instead of an unhandled exception escaping to PegaProx's
# catch-all.
# ---------------------------------------------------------------------------

def test_config_save_handler_rejects_malformed_json_body(plugin, tmp_plugin_dir, monkeypatch):
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: None)
    resp, status = routes_api.config_save_handler()
    assert status == 400
    _, payload = resp
    assert 'JSON' in payload['error']


def test_instances_test_handler_rejects_malformed_json_body(plugin, tmp_plugin_dir, monkeypatch):
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: None)
    resp, status = routes_api.instances_test_handler()
    assert status == 400
    _, payload = resp
    assert 'JSON' in payload['error']


def test_config_save_handler_reports_500_on_disk_error(plugin, tmp_plugin_dir, monkeypatch):
    monkeypatch.setattr(routes_api.request, 'get_json',
                         lambda silent=False: {'instances': [], 'poll': {}})

    def failing_save(path, cfg):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr(config_store, 'save_config', failing_save)
    resp, status = routes_api.config_save_handler()
    assert status == 500
    _, payload = resp
    assert 'save config' in payload['error']
