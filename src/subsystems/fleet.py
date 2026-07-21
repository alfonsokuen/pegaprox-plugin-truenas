# -*- coding: utf-8 -*-
"""Fleet Overview (F3): fan out over every configured instance concurrently
and combine system/pools/services/alerts plus a merged, human-relevant
recent-activity feed. Answers "what's the state of ALL my TrueNAS instances
at a glance" without picking one from the dropdown first.

Concurrency safety: ``TrueNASWSClient.call()`` is documented "not
thread-hostile" (each call gets its own request id and waits only for its
own response) and ``ConnectionManager._get_or_create`` creates/caches a
client per ``(instance_id, 'ro'|'rw')`` under a single lock — concurrent
fan-out across DIFFERENT instances never races. Each instance is also
wrapped in its own try/except so one unreachable/hung appliance degrades to
``reachable: false`` instead of taking the whole Fleet response down with
it, and every RPC within an instance uses ``safe_call`` (brief §4.3/§9
pattern) so e.g. a failing ``service.query`` never hides that instance's
otherwise-healthy pools.

Every aggregate below is a real sum/count over data ``pool.query``/
``system.info``/``service.query`` actually return — no invented metric
(e.g. no "top memory consumers": TrueNAS's ``system.info`` carries no RAM
utilization field to aggregate honestly, so it is not shown here; see the
plan's note to add real telemetry from ``reporting.*`` in a later,
explicitly-deferred phase).

Recent activity — real ``audit.query`` shape confirmed live 2026-07-20
against a real TrueNAS-25.10.1 instance under the plugin's existing RO key
(no privilege change needed): filtering out ``event in (AUTHENTICATION,
LOGOUT)`` is REQUIRED, not optional — those two dominate every instance's
raw feed with noise from the plugin's own RO/RW polling logins (a naive,
unfiltered feed was ~100% self-noise in the live check). What survives that
filter is genuinely actionable: a human (e.g. ``alfonso``) calling something
directly from TrueNAS's own admin UI, or ``svc-pegaprox-rw`` recording this
plugin's own writes — exactly the "did someone bypass the plugin" signal
this feed exists to surface.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.errors import TrueNASError
from core.subsystem import safe_call
from subsystems.pools import list_pools
from subsystems.services import is_service_unhealthy, list_services
from subsystems.system import alerts as system_alerts
from subsystems.system import info as system_info

log = logging.getLogger('plugin.truenas.fleet')

MAX_WORKERS = 6
PER_CALL_TIMEOUT_S = 8.0
ACTIVITY_LIMIT = 5
_UNHEALTHY_ALERT_LEVELS = {'ERROR', 'CRITICAL', 'ALERT', 'EMERGENCY'}
# Confirmed live: two ANDed `!=` filters, not an untested `nin`/`not in`
# operator — this exact combination was the one actually run against .64.
_ACTIVITY_FILTERS = [['event', '!=', 'AUTHENTICATION'], ['event', '!=', 'LOGOUT']]


def _instance_activity(conn):
    options = {
        'order_by': ['-timestamp'], 'limit': ACTIVITY_LIMIT,
        'select': ['timestamp', 'username', 'event', 'service', 'success'],
    }
    return conn.call(
        'audit.query', [{'query-filters': _ACTIVITY_FILTERS, 'query-options': options}],
        timeout=PER_CALL_TIMEOUT_S) or []


def _pool_usage_rows(inst, pools):
    rows = []
    for p in pools:
        size = p.get('size')
        if not size:
            continue
        used = p.get('allocated') or 0
        rows.append({
            'instance_id': inst['id'], 'instance_name': inst.get('name', inst['id']),
            'pool': p.get('name'), 'used': used, 'size': size,
            'pct': round(100 * used / size, 1),
        })
    return rows


def _fetch_instance(inst, get_conn):
    """One instance's Fleet card. Never raises — every failure mode
    (unreachable appliance, one hung RPC) degrades into the returned dict
    rather than aborting the whole fan-out."""
    base = {
        'id': inst['id'], 'name': inst.get('name', inst['id']),
        'client_id': inst.get('client_id', 'unassigned'),
        'reachable': True, 'connect_error': None,
    }
    try:
        conn = get_conn(inst)
    except TrueNASError as e:
        base['reachable'] = False
        base['connect_error'] = str(e)
        return base

    info, info_error = safe_call(
        'system.info', lambda: system_info(conn), {})
    active_alerts, alerts_error = safe_call(
        'alert.list', lambda: system_alerts(conn), [])
    pools, pools_error = safe_call(
        'pool.query', lambda: list_pools(conn), [])
    svcs, svcs_error = safe_call(
        'service.query', lambda: list_services(conn), [])
    activity, activity_error = safe_call(
        'audit.query', lambda: _instance_activity(conn), [])

    unhealthy_pools = [p.get('name') for p in pools if not p.get('healthy', True)]
    down_services = [s.get('service') for s in svcs if is_service_unhealthy(s)]
    critical_alerts = [
        a for a in active_alerts
        if not a.get('dismissed') and str(a.get('level', '')).upper() in _UNHEALTHY_ALERT_LEVELS
    ]
    used = sum(int(p.get('allocated') or 0) for p in pools)
    size = sum(int(p.get('size') or 0) for p in pools)

    base.update({
        'version': info.get('version'),
        'hostname': info.get('hostname'),
        'alert_count': len(active_alerts),
        'critical_alert_count': len(critical_alerts),
        # Raw alert objects, not just the count — the F4a background
        # poller (core/poller.py) needs each alert's own id to dedup a
        # relayed notification against TrueNAS's own alert lifecycle
        # (fires once when it first appears, once when it clears), which a
        # bare count can't support.
        'active_alerts': active_alerts,
        'pool_count': len(pools),
        'unhealthy_pools': unhealthy_pools,
        'capacity_used': used,
        'capacity_size': size,
        'down_services': down_services,
        'activity': activity,
        'pool_usage': _pool_usage_rows(inst, pools),
        'errors': {k: v for k, v in {
            'info': info_error, 'alerts': alerts_error, 'pools': pools_error,
            'services': svcs_error, 'activity': activity_error,
        }.items() if v},
    })
    return base


def fetch_fleet(instances, get_conn):
    """Fetch every configured instance concurrently. ``get_conn(inst)`` is
    the caller's connection resolver (routes/api.py's
    ``_get_authenticated_connection`` in production; a fake in tests) —
    fleet.py never imports ``ConnectionManager`` directly so it stays
    testable without any real socket."""
    results = [None] * len(instances)
    if not instances:
        return results
    workers = min(MAX_WORKERS, len(instances))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(_fetch_instance, inst, get_conn): i
            for i, inst in enumerate(instances)
        }
        for future in as_completed(future_map):
            i = future_map[future]
            try:
                results[i] = future.result()
            except Exception as e:
                inst = instances[i]
                log.error(f"[truenas] fleet fetch crashed for instance "
                          f"'{inst.get('id')}': {e}", exc_info=True)
                results[i] = {
                    'id': inst.get('id'), 'name': inst.get('name', inst.get('id')),
                    'client_id': inst.get('client_id', 'unassigned'),
                    'reachable': False, 'connect_error': f'unexpected error: {e}',
                }
    return results


def aggregate(instance_summaries):
    """Fleet-wide totals from real per-instance data — see module docstring
    on why there is no invented "top memory consumers"-style metric."""
    reachable = [s for s in instance_summaries if s.get('reachable')]
    unreachable = [s for s in instance_summaries if not s.get('reachable')]
    degraded = [
        s for s in reachable
        if s.get('unhealthy_pools') or s.get('critical_alert_count') or s.get('down_services')
    ]

    total_used = sum(s.get('capacity_used', 0) for s in reachable)
    total_size = sum(s.get('capacity_size', 0) for s in reachable)
    total_alerts = sum(s.get('alert_count', 0) for s in reachable)

    all_pool_usage = [row for s in reachable for row in s.get('pool_usage', [])]
    top_pools = sorted(all_pool_usage, key=lambda r: r['pct'], reverse=True)[:5]

    all_activity = [
        dict(row, instance_id=s['id'], instance_name=s.get('name', s['id']))
        for s in reachable for row in s.get('activity', [])
    ]
    all_activity.sort(key=lambda a: (a.get('timestamp') or {}).get('$date', 0), reverse=True)

    return {
        'instance_count': len(instance_summaries),
        'healthy_count': len(reachable) - len(degraded),
        'degraded_count': len(degraded),
        'unreachable_count': len(unreachable),
        'capacity_used': total_used,
        'capacity_size': total_size,
        'capacity_pct': round(100 * total_used / total_size, 1) if total_size else None,
        'total_alerts': total_alerts,
        'top_pools': top_pools,
        'activity': all_activity[:ACTIVITY_LIMIT],
    }
