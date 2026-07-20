# -*- coding: utf-8 -*-
"""Route handlers: config GET/save (masking + grouping), instances/test,
and F1's subsystem read routes — against the stubbed flask.request from
conftest.py."""

from core.errors import TrueNASConnectionError
from routes import api as routes_api
from routes import config_store
from tests.unit.fakes import FakeConn


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


# ---------------------------------------------------------------------------
# F1: subsystem read routes (system/pools/datasets/snapshots/shares/
# replication/apps_vms) — instance_id as a query param (see api.py module
# docstring for why: the only confirmed plugin routing mechanism doesn't
# support URL path parameters).
# ---------------------------------------------------------------------------

def _seed_instance(plugin_dir_cfg_path, **overrides):
    inst = _instance()
    inst.update(overrides)
    config_store.save_config(plugin_dir_cfg_path, {'instances': [inst], 'poll': config_store.DEFAULT_POLL})
    return inst


def test_subsystem_route_requires_instance_id(plugin, tmp_plugin_dir, monkeypatch):
    monkeypatch.setattr(routes_api.request, 'args', {})
    resp, status = routes_api.system_handler()
    assert status == 400
    _, payload = resp
    assert 'instance_id' in payload['error']


def test_subsystem_route_404s_for_unknown_instance(plugin, tmp_plugin_dir, monkeypatch):
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'ghost'})
    resp, status = routes_api.system_handler()
    assert status == 404


def test_subsystem_route_400s_when_instance_has_no_ro_key(plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH, api_key_ro=None)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    resp, status = routes_api.system_handler()
    assert status == 400
    _, payload = resp
    assert 'api_key_ro' in payload['error']


def test_system_handler_returns_info_alerts_health(plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    fake_conn = FakeConn({
        'system.info': {'version': '25.10.1', 'hostname': 'truenas1'},
        'alert.list': [],
        'update.status': {'status': 'AVAILABLE'},
    })
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    resp = routes_api.system_handler()
    _, payload = resp
    assert payload['data']['info']['version'] == '25.10.1'
    assert payload['data']['health']['healthy'] is True
    assert fake_conn.login_calls == []  # already authenticated -> no relogin


def test_subsystem_route_logs_in_when_not_yet_authenticated(plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    fake_conn = FakeConn({
        'system.info': {}, 'alert.list': [], 'update.status': {},
    }, is_authenticated=False)
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    routes_api.system_handler()
    assert fake_conn.login_calls == ['real-secret-ro']  # the RO key, never RW


def test_subsystem_route_reports_truenas_error_with_context_not_bare_500(
        plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})

    def boom(inst):
        raise TrueNASConnectionError('appliance unreachable')

    monkeypatch.setattr(routes_api, '_get_authenticated_connection', boom)
    resp, status = routes_api.system_handler()
    assert status == 502
    _, payload = resp
    assert 'appliance unreachable' in payload['error']
    assert payload['instance_id'] == 'truenas-test'


def test_pools_handler_excludes_degraded_pool_disks_from_temperatures(
        plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    healthy_pool = {'name': 'tank', 'status': 'ONLINE', 'healthy': True,
                     'topology': {'data': [{'disk': 'sda', 'children': []}]},
                     'scan': {'state': 'FINISHED'}}
    degraded_pool = {'name': 'Backup_Proxmox', 'status': 'DEGRADED', 'healthy': False,
                      'topology': {'data': [{'disk': 'sdb', 'children': []}]},
                      'scan': {'state': 'FINISHED'}}
    fake_conn = FakeConn({
        'pool.query': [healthy_pool, degraded_pool],
        'disk.query': [{'name': 'sda'}, {'name': 'sdb'}],
        'disk.temperature_agg': {'sda': {'avg': 29}},
    })
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    resp = routes_api.pools_handler()
    _, payload = resp
    assert payload['data']['temperatures'] == {'sda': {'avg': 29}}
    assert payload['data']['health']['healthy'] is False
    temp_calls = [c for c in fake_conn.calls if c[0] == 'disk.temperature_agg']
    assert temp_calls[0][1] == [['sda']]


def test_datasets_handler_returns_list(plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    fake_conn = FakeConn({'pool.dataset.query': [{'id': 'tank/data'}]})
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    resp = routes_api.datasets_handler()
    _, payload = resp
    assert payload['data'] == [{'id': 'tank/data'}]


def test_snapshots_handler_returns_snapshots_and_tasks(plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    fake_conn = FakeConn({
        'pool.snapshot.query': [{'id': 's1'}],
        'pool.snapshottask.query': [{'id': 1}],
    })
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    resp = routes_api.snapshots_handler()
    _, payload = resp
    assert payload['data']['snapshots'] == [{'id': 's1'}]
    assert payload['data']['tasks'] == [{'id': 1}]


def test_shares_handler_returns_all_kinds(plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    fake_conn = FakeConn({
        'sharing.smb.query': [], 'sharing.nfs.query': [],
        'iscsi.target.query': [], 'iscsi.extent.query': [], 'iscsi.targetextent.query': [],
    })
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    resp = routes_api.shares_handler()
    _, payload = resp
    assert set(payload['data'].keys()) == {
        'smb', 'nfs', 'iscsi_targets', 'iscsi_extents', 'iscsi_targetextents'}


def test_replication_handler_returns_list(plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    fake_conn = FakeConn({'replication.query': [{'id': 1}]})
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    resp = routes_api.replication_handler()
    _, payload = resp
    assert payload['data'] == [{'id': 1}]


def test_apps_vms_handler_returns_apps_and_vms(plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    fake_conn = FakeConn({'app.query': [{'name': 'plex'}], 'vm.query': []})
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    resp = routes_api.apps_vms_handler()
    _, payload = resp
    assert payload['data'] == {'apps': [{'name': 'plex'}], 'vms': []}
