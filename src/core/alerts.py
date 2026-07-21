# -*- coding: utf-8 -*-
"""F4a alert engine: pure functions, no Flask/threading/network, same split
as ``config_store.py``. Turns fresh per-instance readings into a small list
of notifications, comparing against previously-persisted state so a
notification fires on a genuine TRANSITION, never on every poll a condition
happens to still be true.

Anti-flood is the point of this module, not an afterthought bolted on
later (lesson from the idkpublicitaria remediationBridge flooding
incident — a notifier without this from day one just floods the channel):

- Edge-triggered: fires on ok->warn, warn->crit, crit->warn, ->ok/cleared —
  never on "still warn" by itself.
- Hysteresis: a warn/crit capacity condition only clears back to 'ok' once
  usage drops HYSTERESIS_PCT below the warn threshold, not the instant it
  dips under it — otherwise a value oscillating right at the boundary
  would flap a notification every single poll.
- Cooldown: a condition that STAYS at the same non-ok level (no edge) still
  re-notifies after COOLDOWN_S, so a week-long CRITICAL doesn't go
  permanently silent after its first notification — but at most once per
  cooldown window, not once per poll.
- Global rate cap: at most MAX_PER_HOUR real notifications per rolling
  hour across every instance/metric combined; anything beyond that
  collapses into a single "N alerts suppressed" summary rather than
  silently dropping them with no trace.
- Persisted state (``alerts_state.json``, same atomic-write pattern as
  ``config_store.py``) means a plugin/service restart does not re-fire
  every currently-true condition as if it were brand new.
"""

import json
import logging
import os

log = logging.getLogger('plugin.truenas.alerts')

HYSTERESIS_PCT = 5
COOLDOWN_S = 24 * 3600
MAX_PER_HOUR = 10
RATE_WINDOW_S = 3600

_OK_LEVELS = ('ok', 'cleared')


def default_state():
    return {'conditions': {}, 'rate_limit': {'window_start': 0, 'count': 0}, 'last_run': None}


def load_state(path):
    """Same "missing/corrupt -> safe default, log loud on corrupt" shape as
    ``config_store.load_config`` — a lost/corrupt alerts_state.json is a
    recoverable "start tracking fresh" case, not a reason to crash the
    poller thread."""
    try:
        with open(path) as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError('alerts_state root must be an object')
    except FileNotFoundError:
        return default_state()
    except Exception as e:
        log.error(f"[truenas] alerts state at {path!r} is corrupt/unreadable, "
                  f"starting fresh: {e}", exc_info=True)
        return default_state()
    state.setdefault('conditions', {})
    state.setdefault('rate_limit', {'window_start': 0, 'count': 0})
    state.setdefault('last_run', None)
    return state


def save_state(path, state):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _effective_thresholds(instance, global_thresholds):
    """Python mirror of plugin.html's ``instanceThresholds()`` — each side
    overridden independently, falling back to the global pair per-field."""
    warn = instance.get('warn_pct')
    crit = instance.get('crit_pct')
    return {
        'warn_pct': warn if warn is not None else global_thresholds['warn_pct'],
        'crit_pct': crit if crit is not None else global_thresholds['crit_pct'],
    }


def _next_capacity_level(prev_level, pct, thresholds):
    warn, crit = thresholds['warn_pct'], thresholds['crit_pct']
    if pct >= crit:
        return 'crit'
    if pct >= warn:
        return 'warn'
    if prev_level in ('warn', 'crit') and pct >= (warn - HYSTERESIS_PCT):
        return 'warn'
    return 'ok'


def _step(prev_conditions, key, new_level, now, notif):
    """Advance one tracked condition to ``new_level``, deciding whether it
    crosses the notify-worthy bar. ``notif`` is a dict of the fields a
    resulting notification would carry (instance_id/metric/detail/message)
    — built by the caller since only it knows the human-readable context.
    Returns ``(condition, notification_or_None)``."""
    prev = prev_conditions.get(key, {})
    prev_level = prev.get('level', 'ok')

    should_notify = False
    if prev_level != new_level:
        should_notify = True
    elif new_level not in _OK_LEVELS:
        last = prev.get('last_notified')
        should_notify = last is None or (now - last) >= COOLDOWN_S

    condition = {
        'level': new_level,
        'since': prev.get('since', now) if prev_level == new_level else now,
        'last_notified': now if should_notify else prev.get('last_notified'),
    }
    if not should_notify:
        return condition, None
    return condition, dict(notif, level=new_level, ts=now,
                            transition=f'{prev_level}->{new_level}')


def _apply_rate_limit(notifications, prev_rate_limit, now):
    window_start = prev_rate_limit.get('window_start', 0)
    count = prev_rate_limit.get('count', 0)
    if now - window_start >= RATE_WINDOW_S:
        window_start, count = now, 0

    allowed = []
    suppressed = 0
    for n in notifications:
        if count < MAX_PER_HOUR:
            allowed.append(n)
            count += 1
        else:
            suppressed += 1

    if suppressed:
        allowed.append({
            'instance_id': None, 'metric': 'rate_limit', 'detail': None,
            'level': 'warn', 'ts': now, 'transition': None,
            'message': f'{suppressed} alert(s) suppressed this hour (rate limit reached)',
        })
    return allowed, {'window_start': window_start, 'count': count}


def evaluate(prev_state, now, readings, global_thresholds):
    """``readings`` is a list of per-instance dicts:
    ``{'instance_id', 'instance_name', 'reachable', 'pools': [{'name',
    'used_pct'}], 'ta_alerts': [{'id', 'level', 'formatted'}], 'thresholds'
    (optional per-instance override dict)}``.

    Returns ``(new_state, notifications)`` — ``new_state`` is what the
    caller persists via ``save_state``; ``notifications`` is the (already
    rate-limited) list ready to hand to a notify channel."""
    prev_conditions = prev_state.get('conditions', {})
    new_conditions = {}
    raw_notifications = []

    for reading in readings:
        iid = reading['instance_id']
        iname = reading.get('instance_name', iid)
        # Per-field fallback (an instance may override only warn_pct or
        # only crit_pct), matching plugin.html's instanceThresholds() —
        # NOT a whole-object-or-nothing fallback, which would ignore a
        # partial override entirely.
        thresholds = _effective_thresholds(reading, global_thresholds)

        level = 'ok' if reading.get('reachable') else 'down'
        key = f'{iid}|reachability|'
        condition, notif = _step(prev_conditions, key, level, now, {
            'instance_id': iid, 'metric': 'reachability', 'detail': None,
            'message': f'{iname}: {"reachable again" if level == "ok" else "unreachable"}',
        })
        new_conditions[key] = condition
        if notif:
            raw_notifications.append(notif)

        for pool in reading.get('pools', []):
            pool_name = pool['name']
            key = f'{iid}|capacity|{pool_name}'
            prev_level = prev_conditions.get(key, {}).get('level', 'ok')
            level = _next_capacity_level(prev_level, pool['used_pct'], thresholds)
            condition, notif = _step(prev_conditions, key, level, now, {
                'instance_id': iid, 'metric': 'capacity', 'detail': pool_name,
                'message': f"{iname}: pool '{pool_name}' at {pool['used_pct']:.1f}% used",
            })
            new_conditions[key] = condition
            if notif:
                raw_notifications.append(notif)

        seen_alert_keys = set()
        for alert in reading.get('ta_alerts', []):
            alert_id = alert.get('id')
            if not alert_id:
                continue
            key = f'{iid}|ta_alert|{alert_id}'
            seen_alert_keys.add(key)
            condition, notif = _step(prev_conditions, key, 'active', now, {
                'instance_id': iid, 'metric': 'ta_alert', 'detail': alert_id,
                'message': f"{iname}: {alert.get('formatted', alert_id)}",
            })
            new_conditions[key] = condition
            if notif:
                raw_notifications.append(notif)

        # A previously-active TrueNAS alert no longer present has cleared
        # on TrueNAS's own side. Notify once, then drop it from tracked
        # state entirely — no reason to keep a resolved alert's id around
        # forever, unlike capacity/reachability which are always-present
        # per-instance/per-pool conditions.
        for key, prev in prev_conditions.items():
            if (key.startswith(f'{iid}|ta_alert|') and key not in seen_alert_keys
                    and prev.get('level') == 'active'):
                _, notif = _step(prev_conditions, key, 'cleared', now, {
                    'instance_id': iid, 'metric': 'ta_alert', 'detail': key.split('|')[-1],
                    'message': f'{iname}: a previously-active alert has cleared',
                })
                if notif:
                    raw_notifications.append(notif)

    notifications, rate_limit = _apply_rate_limit(
        raw_notifications, prev_state.get('rate_limit', {}), now)
    new_state = {'conditions': new_conditions, 'rate_limit': rate_limit, 'last_run': now}
    return new_state, notifications
