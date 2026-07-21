# -*- coding: utf-8 -*-
"""apps_vms subsystem: app.query + vm.query, both confirmed live against
the real .64 instance (25.10.1) responding [] — no virt.instance.* shim in
F1 (see module docstring for why it would be speculative here). Each
fetched independently (safe_call) — see module docstring for why vm.query
specifically is the one flagged as unstable across versions."""

import pytest

from core.errors import TrueNASConnectionError
from subsystems import apps_vms
from tests.unit.fakes import FakeConn


def test_list_calls_both_app_query_and_vm_query():
    conn = FakeConn({'app.query': [{'name': 'plex'}], 'vm.query': []})
    result = apps_vms.apps_vms.list(conn)
    assert result == {'apps': [{'name': 'plex'}], 'apps_error': None,
                       'vms': [], 'vms_error': None}
    assert set(conn.methods_called()) == {'app.query', 'vm.query'}


def test_failing_vm_query_does_not_hide_working_apps():
    conn = FakeConn({
        'app.query': [{'name': 'plex'}],
        'vm.query': TrueNASConnectionError('vm.query errored on this version'),
    })
    result = apps_vms.apps_vms.list(conn)
    assert result['apps'] == [{'name': 'plex'}]
    assert result['apps_error'] is None
    assert result['vms'] == []
    assert 'vm.query errored' in result['vms_error']


def test_list_never_calls_virt_instance_namespace():
    conn = FakeConn({'app.query': [], 'vm.query': []})
    apps_vms.apps_vms.list(conn)
    assert not any(m.startswith('virt.instance') for m in conn.methods_called())


def test_handles_none_responses():
    conn = FakeConn({'app.query': None, 'vm.query': None})
    assert apps_vms.list_apps(conn) == []
    assert apps_vms.list_vms(conn) == []


# ---------------------------------------------------------------------------
# F5: VM start/stop/restart, App start/stop/redeploy — same build/execute
# pattern as datasets/snapshots/services (brief §5). Method names/param
# shapes confirmed live via core.get_methods (vm.start id:int, app.start
# app_name:str).
# ---------------------------------------------------------------------------

def test_build_vm_control_envelope_start():
    method, params = apps_vms.build_vm_control_envelope('start', 12)
    assert method == 'vm.start'
    assert params == [12, {'overcommit': False}]


def test_build_vm_control_envelope_coerces_string_id_to_int():
    method, params = apps_vms.build_vm_control_envelope('start', '12')
    assert params[0] == 12
    assert isinstance(params[0], int)


def test_build_vm_control_envelope_stop():
    method, params = apps_vms.build_vm_control_envelope('stop', 3)
    assert method == 'vm.stop'
    assert params == [3, {'force': False, 'force_after_timeout': False}]


def test_build_vm_control_envelope_restart():
    method, params = apps_vms.build_vm_control_envelope('restart', 3)
    assert method == 'vm.restart'
    assert params == [3]


def test_build_vm_control_envelope_rejects_unknown_op():
    with pytest.raises(ValueError):
        apps_vms.build_vm_control_envelope('frobnicate', 1)


def test_build_vm_control_envelope_rejects_non_integer_id():
    with pytest.raises(ValueError):
        apps_vms.build_vm_control_envelope('start', 'not-a-number')


def test_control_vm_calls_the_exact_envelope_the_builder_produced():
    conn = FakeConn({'vm.start': True})
    result = apps_vms.control_vm(conn, 'start', 12)
    assert result is True
    assert conn.calls == [('vm.start', [12, {'overcommit': False}])]


def test_build_app_control_envelope_start():
    method, params = apps_vms.build_app_control_envelope('start', 'plex')
    assert method == 'app.start'
    assert params == ['plex']


def test_build_app_control_envelope_redeploy():
    method, params = apps_vms.build_app_control_envelope('redeploy', 'plex')
    assert method == 'app.redeploy'
    assert params == ['plex']


def test_build_app_control_envelope_rejects_restart():
    """Apps have no 'restart' op — only 'redeploy' (stop+pull+start), a
    meaningfully heavier operation. Confirmed live: app.restart does not
    exist on TrueNAS-25.10.1."""
    with pytest.raises(ValueError):
        apps_vms.build_app_control_envelope('restart', 'plex')


def test_build_app_control_envelope_rejects_empty_name():
    with pytest.raises(ValueError):
        apps_vms.build_app_control_envelope('start', '')


def test_control_app_calls_the_exact_envelope_the_builder_produced():
    conn = FakeConn({'app.stop': 4242})  # async job id
    result = apps_vms.control_app(conn, 'stop', 'plex')
    assert result == 4242
    assert conn.calls == [('app.stop', ['plex'])]


def test_find_vm_by_id():
    conn = FakeConn({'vm.query': [{'id': 1, 'name': 'a'}, {'id': 2, 'name': 'b'}]})
    found = apps_vms.find_vm(conn, 2)
    assert found['name'] == 'b'


def test_find_vm_returns_none_when_missing():
    conn = FakeConn({'vm.query': [{'id': 1, 'name': 'a'}]})
    assert apps_vms.find_vm(conn, 99) is None


def test_find_app_by_name():
    conn = FakeConn({'app.query': [{'name': 'plex'}, {'name': 'sonarr'}]})
    found = apps_vms.find_app(conn, 'sonarr')
    assert found['name'] == 'sonarr'
