# -*- coding: utf-8 -*-
"""System subsystem: ``system.info``, ``alert.list``, ``update.status``.

Read-only in F1 — ``update.run`` (F5) and any alert-dismiss action are not
implemented; ``Subsystem.write()``'s default raises ``ReadOnlySubsystem``.

Gotcha confirmed by the brief (§9) and NOT re-verified live this session:
``update.check_available`` does not exist in TrueNAS 25.x — ``update.status``
is the only method, with a nested response shape
(``{"status": {"new_version": {...}}}`` when an update is available, or a
different ``status`` string otherwise). Handled defensively below since the
exact nesting for "no update available" wasn't captured as a live fixture.
"""

from core.subsystem import HealthReport, Subsystem

# TrueNAS's AlertLevel enum (api.truenas.com), ordered least->most severe.
# Treated as unhealthy-making: an active (non-dismissed) alert at or above
# ERROR. WARNING/NOTICE/INFO are surfaced but don't flip system health red.
_UNHEALTHY_ALERT_LEVELS = {'ERROR', 'CRITICAL', 'ALERT', 'EMERGENCY'}


def info(conn):
    """``system.info`` — version, hostname, uptime, etc. Returned as-is
    (attrs passthrough)."""
    return conn.call('system.info') or {}


def alerts(conn):
    """``alert.list`` — active alerts. A real .64 run returned 12 elements
    the day this was verified live; NEVER assume an empty list."""
    return conn.call('alert.list') or []


def update_status(conn):
    """``update.status``. Never call ``update.check_available`` — it was
    removed in 25.x (brief §9)."""
    return conn.call('update.status') or {}


class SystemSubsystem(Subsystem):
    SUBSYSTEM_ID = 'system'

    def read(self, conn, id=None):
        return info(conn)

    def list(self, conn):
        """No natural "list" for system info — returns alerts, since that's
        the one system-level collection that IS a list."""
        return alerts(conn)

    def health(self, conn, active_alerts=None):
        """Healthy iff there is no active, non-dismissed alert at ERROR or
        above. Accepts a pre-fetched ``active_alerts`` to avoid a second
        identical RPC call within the same request."""
        active_alerts = active_alerts if active_alerts is not None else alerts(conn)
        bad = [
            a for a in active_alerts
            if not a.get('dismissed') and str(a.get('level', '')).upper() in _UNHEALTHY_ALERT_LEVELS
        ]
        healthy = not bad
        summary = ('no active ERROR+ alerts' if healthy else
                   f"{len(bad)} active alert(s) at ERROR or above")
        return HealthReport(healthy=healthy, summary=summary, details={
            'total_alerts': len(active_alerts),
            'unhealthy_alert_ids': [a.get('uuid') or a.get('id') for a in bad],
        })


system = SystemSubsystem()
