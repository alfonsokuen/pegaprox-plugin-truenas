# -*- coding: utf-8 -*-
"""telemetry subsystem: CPU/memory/network sparkline data via
reporting.get_data. Field shapes (legend index, memory's available-bytes-
not-percentage, interface needing a real identifier) all confirmed live
against .64 before this was written — see module docstring."""

from subsystems import telemetry
from tests.unit.fakes import FakeConn


def _cpu_entry(rows):
    return [{
        'name': 'cpu', 'identifier': 'cpu',
        'legend': ['time', 'cpu', 'cpu0', 'cpu1'],
        'data': rows,
    }]


def _memory_entry(rows):
    return [{
        'name': 'memory', 'identifier': 'memory',
        'legend': ['time', 'available'],
        'data': rows,
    }]


def _interface_entry(rows):
    return [{
        'name': 'interface', 'identifier': 'eno1',
        'legend': ['time', 'received', 'sent'],
        'data': rows,
    }]


def test_cpu_series_picks_the_aggregate_column_not_a_per_core_one():
    conn = FakeConn({'reporting.get_data': _cpu_entry([[100, 5, 1, 2], [101, 7, 0, 3]])})
    result = telemetry.cpu_series(conn)
    assert result == [[100, 5], [101, 7]]


def test_cpu_series_returns_empty_when_no_entry_returned():
    conn = FakeConn({'reporting.get_data': []})
    assert telemetry.cpu_series(conn) == []


def test_memory_series_converts_available_bytes_to_used_percent():
    physmem = 1000
    conn = FakeConn({'reporting.get_data': _memory_entry([[100, 900], [101, 500]])})
    result = telemetry.memory_series(conn, physmem)
    assert result == [[100, 10.0], [101, 50.0]]


def test_memory_series_handles_zero_physmem_without_dividing_by_zero():
    conn = FakeConn({'reporting.get_data': _memory_entry([[100, 900]])})
    result = telemetry.memory_series(conn, 0)
    assert result == [[100, None]]


def test_primary_interface_name_returns_first_configured_interface():
    conn = FakeConn({'interface.query': [{'name': 'eno1np0'}, {'name': 'bond1'}]})
    assert telemetry.primary_interface_name(conn) == 'eno1np0'


def test_primary_interface_name_returns_none_when_no_interfaces():
    conn = FakeConn({'interface.query': []})
    assert telemetry.primary_interface_name(conn) is None


def test_network_series_returns_empty_without_an_interface_name():
    """Live-confirmed: passing identifier=None/'*' to reporting.get_data
    silently returns zero rows rather than erroring — this guards the
    caller-side equivalent (never even issue the call without a real
    name)."""
    conn = FakeConn({})  # no canned reporting.get_data -> would raise if called
    assert telemetry.network_series(conn, None) == []
    assert conn.methods_called() == []


def test_network_series_returns_received_and_sent_columns():
    conn = FakeConn({'reporting.get_data': _interface_entry([[100, 10.5, 20.5]])})
    result = telemetry.network_series(conn, 'eno1')
    assert result == [[100, 10.5, 20.5]]


def test_downsample_leaves_short_series_untouched():
    rows = [[i, i] for i in range(10)]
    assert telemetry._downsample(rows, max_points=120) == rows


def test_downsample_caps_long_series_at_max_points():
    rows = [[i, i] for i in range(3600)]
    result = telemetry._downsample(rows, max_points=120)
    assert len(result) == 120


def test_telemetry_isolates_a_failing_network_series_from_working_cpu_memory():
    from core.errors import TrueNASConnectionError

    class BoomConn:
        def call(self, method, params=None, timeout=None):
            if method == 'reporting.get_data' and params[0][0]['name'] == 'interface':
                raise TrueNASConnectionError('netdata down')
            if method == 'reporting.get_data' and params[0][0]['name'] == 'cpu':
                return _cpu_entry([[100, 5, 1, 2]])
            if method == 'reporting.get_data' and params[0][0]['name'] == 'memory':
                return _memory_entry([[100, 900]])
            if method == 'interface.query':
                return [{'name': 'eno1'}]
            raise AssertionError(f'unexpected call: {method}')

    result = telemetry.telemetry(BoomConn(), physmem=1000)
    assert result['cpu'] == [[100, 5]]
    assert result['memory'] == [[100, 10.0]]
    assert result['network'] == []
    assert 'netdata down' in result['network_error']
