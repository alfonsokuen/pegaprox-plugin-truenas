# -*- coding: utf-8 -*-
"""system subsystem: system.info / alert.list / update.status, health from
active (non-dismissed) ERROR+ alerts. Never calls update.check_available
(removed in TrueNAS 25.x, brief §9)."""

from subsystems import system
from tests.unit.fakes import FakeConn


def _alert(level='WARNING', dismissed=False, uid='a1'):
    return {'uuid': uid, 'level': level, 'dismissed': dismissed, 'formatted': 'x'}


def test_info_calls_system_info_and_passes_through():
    conn = FakeConn({'system.info': {'version': '25.10.1', 'hostname': 'truenas1'}})
    result = system.info(conn)
    assert result == {'version': '25.10.1', 'hostname': 'truenas1'}
    assert conn.methods_called() == ['system.info']


def test_alerts_returns_list_even_when_middleware_returns_none():
    conn = FakeConn({'alert.list': None})
    assert system.alerts(conn) == []


def test_alerts_does_not_assume_empty_list():
    # Real .64 run returned 12 elements the day this was verified live —
    # regression guard against ever hardcoding/assuming 0.
    conn = FakeConn({'alert.list': [_alert() for _ in range(12)]})
    assert len(system.alerts(conn)) == 12


def test_update_status_never_calls_check_available():
    conn = FakeConn({'update.status': {'status': 'AVAILABLE'}})
    system.update_status(conn)
    assert 'update.check_available' not in conn.methods_called()
    assert conn.methods_called() == ['update.status']


def test_health_is_healthy_with_no_alerts():
    conn = FakeConn({'alert.list': []})
    report = system.system.health(conn)
    assert report.healthy is True


def test_health_unhealthy_on_active_critical_alert():
    conn = FakeConn({'alert.list': [_alert(level='CRITICAL', dismissed=False)]})
    report = system.system.health(conn)
    assert report.healthy is False
    assert 'ERROR' in report.summary or 'alert' in report.summary


def test_health_ignores_dismissed_critical_alert():
    conn = FakeConn({'alert.list': [_alert(level='CRITICAL', dismissed=True)]})
    report = system.system.health(conn)
    assert report.healthy is True


def test_health_ignores_warning_level():
    conn = FakeConn({'alert.list': [_alert(level='WARNING', dismissed=False)]})
    report = system.system.health(conn)
    assert report.healthy is True


def test_health_accepts_prefetched_alerts_without_a_second_call():
    conn = FakeConn({})  # no canned alert.list -> would raise if called again
    prefetched = [_alert(level='ERROR', dismissed=False)]
    report = system.system.health(conn, active_alerts=prefetched)
    assert report.healthy is False
    assert conn.methods_called() == []


def test_read_returns_system_info():
    conn = FakeConn({'system.info': {'hostname': 'h'}})
    assert system.system.read(conn) == {'hostname': 'h'}


def test_list_returns_alerts():
    conn = FakeConn({'alert.list': [_alert()]})
    assert system.system.list(conn) == [_alert()]
