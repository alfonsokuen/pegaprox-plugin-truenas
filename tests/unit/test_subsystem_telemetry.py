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


def test_all_interface_names_returns_every_configured_interface():
    conn = FakeConn({'interface.query': [{'name': 'eno1np0'}, {'name': 'bond1'}]})
    assert telemetry.all_interface_names(conn) == ['eno1np0', 'bond1']


def test_all_interface_names_returns_empty_list_when_no_interfaces():
    conn = FakeConn({'interface.query': []})
    assert telemetry.all_interface_names(conn) == []


def test_all_interface_names_skips_entries_with_no_name():
    conn = FakeConn({'interface.query': [{'name': 'eno1'}, {'mtu': 1500}, {'name': ''}]})
    assert telemetry.all_interface_names(conn) == ['eno1']


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
    assert result['interfaces'] == [{'name': 'eno1', 'series': [], 'error': 'netdata down'}]


def test_telemetry_returns_a_card_per_interface_not_just_the_first():
    """Operator request 2026-07-21: a multi-NIC host only ever showed
    'eno1' — the rest were silently dropped by primary_interface_name()
    only ever resolving ifaces[0]."""
    class TwoIfaceConn:
        def call(self, method, params=None, timeout=None):
            if method == 'interface.query':
                return [{'name': 'eno1'}, {'name': 'eno2'}]
            if method == 'reporting.get_data':
                name = params[0][0]['name']
                if name == 'cpu':
                    return _cpu_entry([[100, 5, 1, 2]])
                if name == 'memory':
                    return _memory_entry([[100, 900]])
                if name == 'interface':
                    identifier = params[0][0]['identifier']
                    if identifier == 'eno1':
                        return _interface_entry([[100, 1.0, 1.5]])
                    if identifier == 'eno2':
                        return _interface_entry([[100, 2.0, 2.5]])
            raise AssertionError(f'unexpected call: {method} {params}')

    result = telemetry.telemetry(TwoIfaceConn(), physmem=1000)
    by_name = {i['name']: i['series'] for i in result['interfaces']}
    # Each interface must carry ITS OWN series, not a copy of whichever
    # interface a shared closure variable happened to point at last.
    assert by_name == {'eno1': [[100, 1.0, 1.5]], 'eno2': [[100, 2.0, 2.5]]}


def test_telemetry_isolates_one_failing_interface_from_other_working_interfaces():
    from core.errors import TrueNASConnectionError

    class TwoIfaceConn:
        def call(self, method, params=None, timeout=None):
            if method == 'interface.query':
                return [{'name': 'eno1'}, {'name': 'eno2'}]
            if method == 'reporting.get_data':
                name = params[0][0]['name']
                if name == 'cpu':
                    return _cpu_entry([[100, 5, 1, 2]])
                if name == 'memory':
                    return _memory_entry([[100, 900]])
                if name == 'interface':
                    identifier = params[0][0]['identifier']
                    if identifier == 'eno1':
                        raise TrueNASConnectionError('eno1 down')
                    if identifier == 'eno2':
                        return _interface_entry([[100, 1.0, 2.0]])
            raise AssertionError(f'unexpected call: {method} {params}')

    result = telemetry.telemetry(TwoIfaceConn(), physmem=1000)
    by_name = {i['name']: i for i in result['interfaces']}
    assert by_name['eno1']['series'] == []
    assert by_name['eno1']['error'] == 'eno1 down'
    assert by_name['eno2']['series'] == [[100, 1.0, 2.0]]
    assert by_name['eno2']['error'] is None


def test_telemetry_returns_empty_interfaces_list_when_none_configured():
    class NoIfaceConn:
        def call(self, method, params=None, timeout=None):
            if method == 'interface.query':
                return []
            if method == 'reporting.get_data':
                name = params[0][0]['name']
                if name == 'cpu':
                    return _cpu_entry([[100, 5, 1, 2]])
                if name == 'memory':
                    return _memory_entry([[100, 900]])
            raise AssertionError(f'unexpected call: {method} {params}')

    result = telemetry.telemetry(NoIfaceConn(), physmem=1000)
    assert result['interfaces'] == []
    assert result['interfaces_error'] is None
