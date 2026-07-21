# -*- coding: utf-8 -*-
"""Overview telemetry (CPU/memory/network) — the CPU/memory/network graphs
the operator asked for after seeing TrueNAS's own native dashboard,
explicitly deferred until after the Storage grid ("layout primero,
gráficos después"). Backed by ``reporting.get_data`` (confirmed live
against `.64` under the plugin's existing RO key — already has
``REPORTING_READ``, no privilege change needed).

Real shapes confirmed live before writing any code:
- ``reporting.get_data([{'name': 'cpu'}], {'unit': 'HOUR', 'page': 1})``
  returns one entry per requested graph: ``{name, identifier, legend,
  data, start, end}``. ``legend`` is ``['time', 'cpu', 'cpu0', 'cpu1', ...]``
  — index 1 ('cpu') is the aggregate/all-core percentage, the rest are
  per-core breakdowns this feature has no use for.
- ``memory``'s legend is ``['time', 'available']`` — bytes still free, NOT
  a used-percentage. Converted here to a used% using ``system.info``'s
  ``physmem`` (total bytes), passed in by the caller (routes/api.py's
  system route already fetches ``system.info`` for the Overview tab, so
  this avoids a second identical RPC).
- ``interface``'s legend is ``['time', 'received', 'sent']`` but requires
  a real interface ``identifier`` (confirmed live: passing ``None``/``'*'``
  silently returns zero rows, not an error) — resolved via
  ``interface.query``'s first configured interface. Multi-NIC/bonded setups
  aren't disambiguated here (out of scope for a first pass); the resolved
  name is returned alongside the series so the UI can label it honestly.

A 1-hour window returns ~3600 one-second rows per metric — far more points
than a small Overview sparkline needs or should ship over the wire.
``_downsample`` reduces to at most ``MAX_POINTS`` evenly-spaced samples
server-side, once, rather than sending the raw resolution to every client
on every poll.
"""

from core.subsystem import parallel_safe_calls, safe_call

MAX_POINTS = 120
REPORTING_UNIT = 'HOUR'


def _downsample(rows, max_points=MAX_POINTS):
    if len(rows) <= max_points:
        return rows
    step = len(rows) / max_points
    return [rows[int(i * step)] for i in range(max_points)]


def _get_data(conn, graphs):
    result = conn.call('reporting.get_data', [graphs, {'unit': REPORTING_UNIT, 'page': 1}])
    return result[0] if result else None


def cpu_series(conn):
    entry = _get_data(conn, [{'name': 'cpu'}])
    if not entry:
        return []
    legend = entry.get('legend') or []
    idx = legend.index('cpu') if 'cpu' in legend else 1
    rows = [[row[0], row[idx]] for row in (entry.get('data') or []) if len(row) > idx]
    return _downsample(rows)


def memory_series(conn, physmem):
    entry = _get_data(conn, [{'name': 'memory'}])
    if not entry:
        return []
    rows = []
    for row in (entry.get('data') or []):
        if len(row) < 2:
            continue
        ts, available = row[0], row[1]
        used_pct = round(100 * (physmem - available) / physmem, 1) if physmem else None
        rows.append([ts, used_pct])
    return _downsample(rows)


def primary_interface_name(conn):
    ifaces = conn.call('interface.query') or []
    return ifaces[0].get('name') if ifaces else None


def network_series(conn, iface_name):
    if not iface_name:
        return []
    entry = _get_data(conn, [{'name': 'interface', 'identifier': iface_name}])
    if not entry:
        return []
    rows = [[row[0], row[1], row[2]] for row in (entry.get('data') or []) if len(row) >= 3]
    return _downsample(rows)


def telemetry(conn, physmem=None):
    """Every series fetched independently via ``safe_call`` — a hung/
    erroring network graph must not also hide CPU/memory, same isolation
    rule as every other multi-call subsystem in this plugin.

    ``network`` needs ``iface_name`` first, so it can't join the first
    round — but cpu/memory/interface.query have no such dependency on
    each other and used to pay three sequential round-trips anyway.
    Fetched CONCURRENTLY instead (perf finding 2026-07-21): two RPC
    stages instead of four, since network is the only genuinely
    sequential step."""
    (cpu, cpu_error), (memory, memory_error), (iface_name, iface_error) = parallel_safe_calls([
        ('reporting.get_data(cpu)', lambda: cpu_series(conn), []),
        ('reporting.get_data(memory)', lambda: memory_series(conn, physmem), []),
        ('interface.query', lambda: primary_interface_name(conn), None),
    ])
    network, network_error = safe_call(
        'reporting.get_data(interface)', lambda: network_series(conn, iface_name), [])
    return {
        'cpu': cpu, 'cpu_error': cpu_error,
        'memory': memory, 'memory_error': memory_error,
        'network': network, 'network_error': network_error or iface_error,
        'network_interface': iface_name,
    }
