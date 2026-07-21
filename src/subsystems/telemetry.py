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
  ``interface.query``. Every configured interface gets its own series
  (operator request 2026-07-21: the first pass only ever showed the first
  NIC, silently hiding the rest on multi-NIC/bonded hosts) — each fetched
  independently so one interface's `reporting.get_data` failing doesn't
  blank the others, same isolation rule as cpu/memory/network as a whole.

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


def all_interface_names(conn):
    ifaces = conn.call('interface.query') or []
    return [i['name'] for i in ifaces if i.get('name')]


def network_series(conn, iface_name):
    if not iface_name:
        return []
    entry = _get_data(conn, [{'name': 'interface', 'identifier': iface_name}])
    if not entry:
        return []
    rows = [[row[0], row[1], row[2]] for row in (entry.get('data') or []) if len(row) >= 3]
    return _downsample(rows)


def telemetry(conn, physmem=None):
    """Every series fetched independently via ``safe_call``/
    ``parallel_safe_calls`` — a hung/erroring graph must not also hide the
    others, same isolation rule as every other multi-call subsystem in
    this plugin (now also applied PER INTERFACE, not just per-metric: one
    NIC's `reporting.get_data` failing must not blank the rest).

    Interface series need ``iface_names`` first, so they can't join the
    first round — but cpu/memory/interface.query have no such dependency
    on each other and used to pay three sequential round-trips anyway.
    Fetched CONCURRENTLY instead (perf finding 2026-07-21)."""
    (cpu, cpu_error), (memory, memory_error), (iface_names, iface_names_error) = parallel_safe_calls([
        ('reporting.get_data(cpu)', lambda: cpu_series(conn), []),
        ('reporting.get_data(memory)', lambda: memory_series(conn, physmem), []),
        ('interface.query', lambda: all_interface_names(conn), []),
    ])

    interfaces = []
    if iface_names:
        # `n=name` binds each loop value at lambda-creation time — a bare
        # `lambda: network_series(conn, name)` would close over the loop
        # variable itself, so every thunk would fetch whichever interface
        # happened to be last by the time parallel_safe_calls runs them.
        specs = [
            ('reporting.get_data(interface:%s)' % name, (lambda n=name: network_series(conn, n)), [])
            for name in iface_names
        ]
        for name, (series, error) in zip(iface_names, parallel_safe_calls(specs)):
            interfaces.append({'name': name, 'series': series, 'error': error})

    return {
        'cpu': cpu, 'cpu_error': cpu_error,
        'memory': memory, 'memory_error': memory_error,
        'interfaces': interfaces,
        'interfaces_error': iface_names_error,
    }
