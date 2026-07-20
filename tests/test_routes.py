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


def test_subsystem_route_fails_fast_on_needs_auth_without_retrying_login(
        plugin, tmp_plugin_dir, monkeypatch):
    """A key already known-bad (needs_auth) must not be retried via
    conn.login() on every request/poll — that just hammers the appliance
    with the same doomed call. The route should still surface a clear
    502, just without re-issuing the RPC."""
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    fake_conn = FakeConn({}, is_authenticated=False, needs_auth=True)
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    resp, status = routes_api.system_handler()
    assert status == 502
    _, payload = resp
    assert 'rejected' in payload['error'].lower() or 'rotate' in payload['error'].lower() \
        or 'check' in payload['error'].lower()
    assert fake_conn.login_calls == []


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


def test_pools_handler_survives_temperature_agg_failure(plugin, tmp_plugin_dir, monkeypatch):
    """The real risk scenario (brief §4.3/§9): a disk failing SMART in a
    pool that's STILL ONLINE — exactly where disk.temperature_agg can
    hang/error. That must not also take down pools/disks/health, which is
    what an all-or-nothing fetch used to do (502, everything lost)."""
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    pool = {'name': 'tank', 'status': 'ONLINE', 'healthy': True,
            'topology': {'data': [{'disk': 'sda', 'children': []}]},
            'scan': {'state': 'FINISHED'}}
    fake_conn = FakeConn({
        'pool.query': [pool],
        'disk.query': [{'name': 'sda'}],
        'disk.temperature_agg': TrueNASConnectionError('SMART query hung'),
    })
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    resp = routes_api.pools_handler()
    _, payload = resp
    assert payload['data']['pools'] == [pool]
    assert payload['data']['health']['healthy'] is True
    assert payload['data']['temperatures'] == {}
    assert 'SMART query hung' in payload['data']['temperatures_error']
    assert payload['data']['disks'] == [{'name': 'sda'}]
    assert payload['data']['disks_error'] is None


def test_pools_handler_survives_disk_query_failure(plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    pool = {'name': 'tank', 'status': 'ONLINE', 'healthy': True, 'topology': {}}
    fake_conn = FakeConn({
        'pool.query': [pool],
        'disk.query': TrueNASConnectionError('disk subsystem timeout'),
    })
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    resp = routes_api.pools_handler()
    _, payload = resp
    assert payload['data']['pools'] == [pool]
    assert payload['data']['health']['healthy'] is True
    assert payload['data']['disks'] == []
    assert 'disk subsystem timeout' in payload['data']['disks_error']
    assert payload['data']['temperatures'] == {}  # no disks -> nothing to query


def test_system_handler_survives_update_status_failure(plugin, tmp_plugin_dir, monkeypatch):
    """update.status is the LEAST critical of system's three calls (and
    its "no update available" shape was never captured live per this
    module's own docstring) — its failure must not also hide alerts/health."""
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    fake_conn = FakeConn({
        'system.info': {'version': '25.10.1'},
        'alert.list': [],
        'update.status': TrueNASConnectionError('update.status errored'),
    })
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    resp = routes_api.system_handler()
    _, payload = resp
    assert payload['data']['info'] == {'version': '25.10.1'}
    assert payload['data']['health']['healthy'] is True
    assert payload['data']['update_status'] == {}
    assert 'update.status errored' in payload['data']['update_status_error']


def test_system_handler_survives_alert_list_failure(plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})
    fake_conn = FakeConn({
        'system.info': {'version': '25.10.1'},
        'alert.list': TrueNASConnectionError('alert.list errored'),
        'update.status': {'status': 'AVAILABLE'},
    })
    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', lambda inst: fake_conn)
    resp = routes_api.system_handler()
    _, payload = resp
    assert payload['data']['info'] == {'version': '25.10.1'}
    assert payload['data']['alerts'] == []
    assert 'alert.list errored' in payload['data']['alerts_error']
    assert payload['data']['update_status'] == {'status': 'AVAILABLE'}


def test_subsystem_route_logs_warning_on_truenas_error(plugin, tmp_plugin_dir, monkeypatch, caplog):
    """The 502 path (appliance down/timeout/revoked key) used to leave zero
    server-side trace — at 3am there was no way to correlate 'this instance
    has been 502ing for hours' from the logs."""
    import logging
    _seed_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'args', {'instance_id': 'truenas-test'})

    def boom(inst):
        raise TrueNASConnectionError('appliance unreachable')

    monkeypatch.setattr(routes_api, '_get_authenticated_connection', boom)
    with caplog.at_level(logging.WARNING, logger='plugin.truenas'):
        routes_api.system_handler()
    assert any('appliance unreachable' in r.message for r in caplog.records)
    assert any('truenas-test' in r.message for r in caplog.records)


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
        'smb', 'smb_error', 'nfs', 'nfs_error',
        'iscsi_targets', 'iscsi_targets_error',
        'iscsi_extents', 'iscsi_extents_error',
        'iscsi_targetextents', 'iscsi_targetextents_error',
    }


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
    assert payload['data'] == {'apps': [{'name': 'plex'}], 'apps_error': None,
                                'vms': [], 'vms_error': None}


# ---------------------------------------------------------------------------
# F2: write path (brief §5) — dry-run vs execute MUST use the identical
# builder (never diverge), readonly/api_key_rw gates, typed confirmation,
# post-write verify (ok/pending/verify_failed), audit.
# ---------------------------------------------------------------------------

def _writable_instance(plugin_dir_cfg_path, **overrides):
    defaults = {'readonly': False, 'api_key_rw': 'real-secret-rw'}
    defaults.update(overrides)
    return _seed_instance(plugin_dir_cfg_path, **defaults)


def test_writes_dry_run_returns_method_and_params_without_touching_conn_manager(
        plugin, tmp_plugin_dir, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError('dry-run must never touch conn_manager')

    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', boom)
    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', boom)
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'subsystem': 'datasets', 'op': 'create',
        'payload': {'name': 'tank/test-dataset', 'type': 'FILESYSTEM'},
    })
    resp = routes_api.writes_dry_run_handler()
    _, payload = resp
    assert payload['method'] == 'pool.dataset.create'
    assert payload['params'] == [{'name': 'tank/test-dataset', 'type': 'FILESYSTEM'}]


def test_writes_dry_run_rejects_unknown_op(plugin, tmp_plugin_dir, monkeypatch):
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'subsystem': 'datasets', 'op': 'frobnicate', 'payload': {},
    })
    resp, status = routes_api.writes_dry_run_handler()
    assert status == 400


def test_writes_dry_run_dataset_delete_confirmation_mismatch_400(
        plugin, tmp_plugin_dir, monkeypatch):
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'subsystem': 'datasets', 'op': 'delete',
        'payload': {'dataset_id': 'tank/test-dataset', 'confirm_name': 'tank/wrong'},
    })
    resp, status = routes_api.writes_dry_run_handler()
    assert status == 400
    _, payload = resp
    assert 'confirmation mismatch' in payload['error']


def test_writes_dry_run_and_execute_use_identical_builder(plugin, tmp_plugin_dir, monkeypatch):
    """The core anti-divergence guarantee: for the SAME payload, dry-run's
    (method, params) must be byte-for-byte identical to what execute
    actually calls conn.call() with."""
    _writable_instance(routes_api.CONFIG_PATH)
    dry_run_payload = {
        'subsystem': 'snapshots', 'op': 'create',
        'payload': {'dataset': 'tank/test-dataset', 'name': 'snap1'},
    }
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: dry_run_payload)
    dry_resp = routes_api.writes_dry_run_handler()
    _, dry_payload = dry_resp

    fake_conn = FakeConn({'pool.snapshot.create': {'id': 'tank/test-dataset@snap1'}})
    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', lambda inst: fake_conn)
    monkeypatch.setattr(routes_api.snapshots_subsystem, 'read',
                         lambda conn, id: {'id': id})  # verify: pretend it now exists
    execute_body = dict(dry_run_payload)
    execute_body['instance_id'] = 'truenas-test'
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: execute_body)
    routes_api.writes_execute_handler()

    method, params = fake_conn.calls[0]
    assert method == dry_payload['method']
    assert params == dry_payload['params']


def test_writes_execute_requires_instance_id(plugin, tmp_plugin_dir, monkeypatch):
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'subsystem': 'datasets', 'op': 'create', 'payload': {'name': 'tank/test-dataset'},
    })
    resp, status = routes_api.writes_execute_handler()
    assert status == 400


def test_writes_execute_403_when_instance_is_readonly(plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH, readonly=True, api_key_rw='real-secret-rw')
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'create',
        'payload': {'name': 'tank/test-dataset'},
    })
    resp, status = routes_api.writes_execute_handler()
    assert status == 403
    _, payload = resp
    assert 'readonly' in payload['error']


def test_writes_execute_403_when_no_rw_key_configured(plugin, tmp_plugin_dir, monkeypatch):
    _seed_instance(routes_api.CONFIG_PATH, readonly=False, api_key_rw=None)
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'create',
        'payload': {'name': 'tank/test-dataset'},
    })
    resp, status = routes_api.writes_execute_handler()
    assert status == 403
    _, payload = resp
    assert 'api_key_rw' in payload['error']


def test_writes_execute_rejects_confirmation_mismatch_before_touching_conn(
        plugin, tmp_plugin_dir, monkeypatch):
    _writable_instance(routes_api.CONFIG_PATH)

    def boom(inst):
        raise AssertionError('must not connect when confirmation is invalid')

    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', boom)
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'delete',
        'payload': {'dataset_id': 'tank/test-dataset', 'confirm_name': 'tank/wrong-name'},
    })
    resp, status = routes_api.writes_execute_handler()
    assert status == 400
    _, payload = resp
    assert 'confirmation mismatch' in payload['error']


def test_writes_execute_dataset_create_success_verifies_and_audits(
        plugin, tmp_plugin_dir, monkeypatch):
    _writable_instance(routes_api.CONFIG_PATH)
    fake_conn = FakeConn({'pool.dataset.create': {'id': 'tank/test-dataset'}}, is_authenticated=False)
    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', lambda inst: fake_conn)
    monkeypatch.setattr(routes_api.datasets_subsystem, 'read',
                         lambda conn, id: {'id': id} if id == 'tank/test-dataset' else None)
    audited = []
    monkeypatch.setattr(routes_api, 'log_audit',
                         lambda **kw: audited.append(kw))
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'create',
        'payload': {'name': 'tank/test-dataset', 'type': 'FILESYSTEM'},
    })
    resp = routes_api.writes_execute_handler()
    _, payload = resp
    assert payload['ok'] is True
    assert payload['status'] == 'ok'
    assert payload['method'] == 'pool.dataset.create'
    assert fake_conn.login_calls == ['real-secret-rw']
    assert len(audited) == 1
    assert 'idkmanager' in audited[0]['details']  # client_id folded into details
    assert 'truenas.datasets.create' == audited[0]['action']


def test_writes_execute_reports_pending_when_job_id_and_verify_not_yet_matching(
        plugin, tmp_plugin_dir, monkeypatch):
    """Conservative design for the unresolved sync-vs-async question: an
    int result + a verify that doesn't show the expected state yet must
    report 'pending', never a false 'ok' or a false 'verify_failed'."""
    _writable_instance(routes_api.CONFIG_PATH)
    fake_conn = FakeConn({'pool.dataset.create': 12345})  # looks like a job id
    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', lambda inst: fake_conn)
    monkeypatch.setattr(routes_api.datasets_subsystem, 'read', lambda conn, id: None)
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'create',
        'payload': {'name': 'tank/test-dataset'},
    })
    resp = routes_api.writes_execute_handler()
    _, payload = resp
    assert payload['status'] == 'pending'
    assert payload['ok'] is False
    assert payload['job_id'] == 12345


def test_writes_execute_reports_verify_failed_when_no_job_id_and_verify_mismatch(
        plugin, tmp_plugin_dir, monkeypatch):
    _writable_instance(routes_api.CONFIG_PATH)
    fake_conn = FakeConn({'pool.dataset.create': {'id': 'tank/test-dataset'}}, is_authenticated=False)
    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', lambda inst: fake_conn)
    # The create call "succeeded" per TrueNAS but the dataset doesn't
    # actually show up on re-read — a real, surfaced problem.
    monkeypatch.setattr(routes_api.datasets_subsystem, 'read', lambda conn, id: None)
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'create',
        'payload': {'name': 'tank/test-dataset'},
    })
    resp = routes_api.writes_execute_handler()
    _, payload = resp
    assert payload['status'] == 'verify_failed'
    assert payload['ok'] is False


def test_writes_execute_502_on_truenas_error_during_call(plugin, tmp_plugin_dir, monkeypatch):
    _writable_instance(routes_api.CONFIG_PATH)
    fake_conn = FakeConn({'pool.dataset.create': TrueNASConnectionError('appliance unreachable')})
    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', lambda inst: fake_conn)
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'create',
        'payload': {'name': 'tank/test-dataset'},
    })
    resp, status = routes_api.writes_execute_handler()
    assert status == 502
    _, payload = resp
    assert payload['ok'] is False
    assert 'appliance unreachable' in payload['error']


def test_writes_execute_dataset_delete_success(plugin, tmp_plugin_dir, monkeypatch):
    _writable_instance(routes_api.CONFIG_PATH)
    fake_conn = FakeConn({'pool.dataset.delete': True})
    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', lambda inst: fake_conn)
    monkeypatch.setattr(routes_api.datasets_subsystem, 'read', lambda conn, id: None)  # gone
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'delete',
        'payload': {'dataset_id': 'tank/test-dataset', 'confirm_name': 'tank/test-dataset'},
    })
    resp = routes_api.writes_execute_handler()
    _, payload = resp
    assert payload['status'] == 'ok'
    assert payload['method'] == 'pool.dataset.delete'


def test_writes_execute_uses_rw_connection_not_ro(plugin, tmp_plugin_dir, monkeypatch):
    """Writes must go through get_rw_connection(), never get_connection()
    (the shared read-only cache) — reusing the RO client for a write would
    silently upgrade every subsequent read to an RW-privileged session."""
    _writable_instance(routes_api.CONFIG_PATH)
    fake_conn = FakeConn({'pool.dataset.create': {'id': 'tank/test-dataset'}}, is_authenticated=False)
    ro_conn_touched = {'value': False}

    def fake_get_connection(inst):
        ro_conn_touched['value'] = True
        return fake_conn

    monkeypatch.setattr(routes_api.conn_manager, 'get_connection', fake_get_connection)
    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', lambda inst: fake_conn)
    monkeypatch.setattr(routes_api.datasets_subsystem, 'read',
                         lambda conn, id: {'id': id})
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'create',
        'payload': {'name': 'tank/test-dataset'},
    })
    routes_api.writes_execute_handler()
    assert ro_conn_touched['value'] is False
    assert fake_conn.login_calls == ['real-secret-rw']


def test_writes_execute_dataset_update_success(plugin, tmp_plugin_dir, monkeypatch):
    _writable_instance(routes_api.CONFIG_PATH)
    fake_conn = FakeConn({'pool.dataset.update': True}, is_authenticated=False)
    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', lambda inst: fake_conn)
    monkeypatch.setattr(routes_api.datasets_subsystem, 'read',
                         lambda conn, id: {'id': id, 'volsize': 4096})
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'update',
        'payload': {'dataset_id': 'tank/test-dataset',
                    'changes': {'volsize': 4096, 'force_size': True}},
    })
    resp = routes_api.writes_execute_handler()
    _, payload = resp
    assert payload['status'] == 'ok'
    assert payload['method'] == 'pool.dataset.update'
    method, params = fake_conn.calls[0]
    assert params == ['tank/test-dataset', {'volsize': 4096, 'force_size': True}]


def test_writes_execute_snapshot_delete_success(plugin, tmp_plugin_dir, monkeypatch):
    _writable_instance(routes_api.CONFIG_PATH)
    fake_conn = FakeConn({'pool.snapshot.delete': True}, is_authenticated=False)
    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', lambda inst: fake_conn)
    monkeypatch.setattr(routes_api.snapshots_subsystem, 'read', lambda conn, id: None)  # gone
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'snapshots', 'op': 'delete',
        'payload': {'snapshot_id': 'tank/test-dataset@snap1',
                    'confirm_name': 'tank/test-dataset@snap1'},
    })
    resp = routes_api.writes_execute_handler()
    _, payload = resp
    assert payload['status'] == 'ok'
    assert payload['method'] == 'pool.snapshot.delete'


def test_writes_execute_snapshot_delete_confirmation_mismatch(plugin, tmp_plugin_dir, monkeypatch):
    _writable_instance(routes_api.CONFIG_PATH)

    def boom(inst):
        raise AssertionError('must not connect when confirmation is invalid')

    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', boom)
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'snapshots', 'op': 'delete',
        'payload': {'snapshot_id': 'tank/test-dataset@snap1', 'confirm_name': 'wrong'},
    })
    resp, status = routes_api.writes_execute_handler()
    assert status == 400


def test_writes_execute_reports_unexpected_error_as_500(plugin, tmp_plugin_dir, monkeypatch):
    _writable_instance(routes_api.CONFIG_PATH)

    def boom(inst):
        raise ValueError('a genuine bug, not a TrueNAS error')

    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', boom)
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'create',
        'payload': {'name': 'tank/test-dataset'},
    })
    resp, status = routes_api.writes_execute_handler()
    assert status == 500
    _, payload = resp
    assert payload['ok'] is False


def test_writes_execute_verify_error_reports_verify_failed_not_a_crash(
        plugin, tmp_plugin_dir, monkeypatch):
    """A TrueNASError during the post-write verify read must not crash the
    route — the write itself already succeeded per TrueNAS; verify just
    couldn't confirm it, which is exactly 'verify_failed'."""
    _writable_instance(routes_api.CONFIG_PATH)
    fake_conn = FakeConn({'pool.dataset.create': {'id': 'tank/test-dataset'}},
                         is_authenticated=False)
    monkeypatch.setattr(routes_api.conn_manager, 'get_rw_connection', lambda inst: fake_conn)

    def verify_boom(conn, id):
        raise TrueNASConnectionError('verify read timed out')

    monkeypatch.setattr(routes_api.datasets_subsystem, 'read', verify_boom)
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'create',
        'payload': {'name': 'tank/test-dataset'},
    })
    resp = routes_api.writes_execute_handler()
    _, payload = resp
    assert payload['status'] == 'verify_failed'
    assert payload['ok'] is False


def test_writes_execute_unknown_op_400(plugin, tmp_plugin_dir, monkeypatch):
    _writable_instance(routes_api.CONFIG_PATH)
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'instance_id': 'truenas-test', 'subsystem': 'datasets', 'op': 'frobnicate',
        'payload': {},
    })
    resp, status = routes_api.writes_execute_handler()
    assert status == 400


def test_writes_dry_run_rejects_malformed_json_body(plugin, tmp_plugin_dir, monkeypatch):
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: None)
    resp, status = routes_api.writes_dry_run_handler()
    assert status == 400


def test_writes_execute_rejects_malformed_json_body(plugin, tmp_plugin_dir, monkeypatch):
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: None)
    resp, status = routes_api.writes_execute_handler()
    assert status == 400


def test_writes_dry_run_dataset_update_validation_error(plugin, tmp_plugin_dir, monkeypatch):
    monkeypatch.setattr(routes_api.request, 'get_json', lambda silent=False: {
        'subsystem': 'datasets', 'op': 'update',
        'payload': {'dataset_id': 'tank/test-dataset', 'changes': {}},
    })
    resp, status = routes_api.writes_dry_run_handler()
    assert status == 400
