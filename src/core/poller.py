# -*- coding: utf-8 -*-
"""F4a background poller: a single daemon thread, started once from
``__init__.py``'s ``register()``, that periodically fans out over every
configured instance (reusing ``fetch_fleet`` — the same concurrent,
per-instance-isolated fetch Fleet's own route already relies on) and feeds
the results through ``core/alerts.py``'s edge-triggered evaluator.

Not architecture foreign to this plugin: ``core/ws_client.py`` already runs
its own daemon threads for read-loop and background-reconnect (see
``_background_reconnect``) — this is the same ``threading.Thread(...,
daemon=True)`` + guard-flag pattern, one level up.

Every poll cycle is wrapped in its own try/except: an exception here must
NEVER kill the thread silently (that would be exactly the kind of silent
failure this whole feature exists to catch for TrueNAS itself). A crashed
cycle logs loudly, records itself in ``status()`` as not-ok, and the loop
continues to the next cycle on the configured cadence.
"""

import logging
import threading
import time

from . import alerts, notify
from routes import config_store
from subsystems.fleet import fetch_fleet

log = logging.getLogger('plugin.truenas.poller')

_lock = threading.Lock()
_thread = None
_stop_event = threading.Event()
_status_lock = threading.Lock()
_status = {'last_run': None, 'ok': None, 'error': None, 'notifications_sent': 0}


def start(config_path, state_path, get_conn):
    """``get_conn(inst) -> authenticated client`` — callers pass
    ``routes.api._get_authenticated_connection`` (imported the other way
    around: ``api.py`` does ``from core import poller``, so this module
    must never import FROM ``routes.api`` or it'd be circular). Passing
    the function itself rather than the raw ``ConnectionManager`` means
    the poller shares the SAME login state as browser-triggered reads —
    calling ``conn_manager.get_connection`` directly here would return an
    unauthenticated client and every RPC would fail with
    ``ENOTAUTHENTICATED`` (a real bug caught live on the first deploy).

    Idempotent: a second call while the poller is already running logs
    and returns rather than spawning a duplicate worker — same guard shape
    as ``TrueNASWSClient._reconnecting``."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            log.warning('[truenas] poller already running, ignoring duplicate start()')
            return
        _stop_event.clear()
        _thread = threading.Thread(
            target=_loop, args=(config_path, state_path, get_conn),
            name='truenas-poller', daemon=True)
        _thread.start()
        log.info('[truenas] background poller started')


def stop(timeout=5):
    """Used by tests (and a clean plugin shutdown, if PegaProx ever calls
    one) to bring the loop down deterministically instead of leaving a
    daemon thread to be killed with the process."""
    _stop_event.set()
    with _lock:
        thread = _thread
    if thread is not None:
        thread.join(timeout=timeout)


def status():
    """Backs the Settings tab's "poller alive Xs ago" badge — the
    anti-silent-failure observability the poller itself needs, mirroring
    why every RPC in this plugin goes through ``safe_call`` instead of a
    bare try/except that swallows and forgets."""
    with _status_lock:
        return dict(_status)


def _set_status(ok, error, notifications_sent=0):
    with _status_lock:
        _status['last_run'] = time.time()
        _status['ok'] = ok
        _status['error'] = error
        _status['notifications_sent'] = notifications_sent


def _loop(config_path, state_path, get_conn):
    while not _stop_event.is_set():
        cadence = run_one_cycle(config_path, state_path, get_conn)
        _stop_event.wait(cadence)


def _summaries_to_readings(instances, summaries):
    inst_by_id = {i['id']: i for i in instances}
    readings = []
    for summary in summaries:
        if summary is None:
            continue
        iid = summary.get('id')
        inst = inst_by_id.get(iid, {})
        pools = [
            {'name': row['pool'], 'used_pct': row['pct']}
            for row in summary.get('pool_usage', [])
        ]
        readings.append({
            'instance_id': iid,
            'instance_name': summary.get('name', iid),
            'reachable': bool(summary.get('reachable')),
            'pools': pools,
            'ta_alerts': summary.get('active_alerts', []),
            'warn_pct': inst.get('warn_pct'),
            'crit_pct': inst.get('crit_pct'),
        })
    return readings


def run_one_cycle(config_path, state_path, get_conn):
    """One poll+evaluate+notify cycle, callable directly (tests call this
    without going through the thread loop at all). Returns the cadence
    (seconds) the loop should sleep before the next cycle — read fresh
    from config every cycle since the operator can change poll.slow_s from
    Settings at any time."""
    try:
        cfg = config_store.load_config(config_path)
        cadence = cfg['poll']['slow_s']
        instances = cfg['instances']
        if not instances:
            _set_status(ok=True, error=None)
            return cadence

        summaries = fetch_fleet(instances, get_conn)
        readings = _summaries_to_readings(instances, summaries)

        prev_state = alerts.load_state(state_path)
        new_state, notifications = alerts.evaluate(
            prev_state, time.time(), readings, cfg['thresholds'])
        alerts.save_state(state_path, new_state)

        if notifications:
            notify_cfg = cfg.get('notify') or {}
            ok, err = notify.send_webhook(notify_cfg.get('webhook_url'), notifications)
            if not ok:
                log.warning(f'[truenas] poller: webhook delivery failed: {err}')
            ok, err = notify.send_whatsapp(
                notify_cfg.get('whatsapp_gateway_url'), notify_cfg.get('whatsapp_instance'),
                notify_cfg.get('whatsapp_api_key'), notify_cfg.get('whatsapp_target'),
                notifications)
            if not ok:
                log.warning(f'[truenas] poller: whatsapp delivery failed: {err}')

        _set_status(ok=True, error=None, notifications_sent=len(notifications))
        return cadence
    except Exception as e:
        log.error(f'[truenas] poller cycle crashed: {e}', exc_info=True)
        _set_status(ok=False, error=str(e))
        return config_store.DEFAULT_POLL['slow_s']
