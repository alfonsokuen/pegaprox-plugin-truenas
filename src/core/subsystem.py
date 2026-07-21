# -*- coding: utf-8 -*-
"""``Subsystem`` contract — brief §2's synthesis of the CloudBridge
``Provider`` interface down to what a single-vendor, multi-instance plugin
actually needs: a uniform ``list/read/health`` shape per TrueNAS concept
(pools, datasets, snapshots, shares, replication, apps_vms, system), with
``write`` read-only by default until F2 wires real writers behind the
dry-run/confirm/audit pattern (brief §5).

Concrete subsystem modules under ``src/subsystems/`` each define a class
implementing this contract PLUS a couple of subsystem-specific helper
functions (e.g. pools' ``temperatures()``) that don't fit the generic
shape — the contract standardizes what routes/tests can rely on across
every subsystem, it doesn't try to cram everything into three methods.

Attrs passthrough (brief §2): responses from the TrueNAS middleware are
returned as-is, never normalized/flattened — the plugin doesn't invent a
lowest-common-denominator shape across subsystems.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .errors import TrueNASError

log = logging.getLogger('plugin.truenas.subsystem')


def parallel_safe_calls(specs):
    """Run several ``safe_call``-shaped ``(label, fn, default)`` specs
    CONCURRENTLY against the same connection, returning ``[(value, error),
    ...]`` in the same order as ``specs``.

    Safe because ``TrueNASWSClient.call()`` is documented as not
    thread-hostile (each call gets its own request id and waits only for
    its own response — see ``fleet.py``, the first place this pattern was
    used, across DIFFERENT instances). Every multi-collection subsystem
    (shares' 5 queries, apps_vms' 2, data_protection's 3, telemetry's 4)
    used to pay N sequential WebSocket round-trips for reads that don't
    depend on each other — this collapses that to the slowest single
    call instead of the sum of all of them, without changing any
    behavior: each spec still degrades independently exactly like a
    sequential ``safe_call`` would.
    """
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        futures = [pool.submit(safe_call, label, fn, default) for label, fn, default in specs]
        return [f.result() for f in futures]


def safe_call(label, fn, default):
    """Call ``fn()`` and return ``(value, error_message_or_None)``.

    On a ``TrueNASError``, degrades to ``default`` and logs a warning
    instead of letting one sub-call's failure sink an entire multi-call
    response. This matters most where a subsystem combines several
    independent TrueNAS collections: e.g. a hung/erroring
    ``disk.temperature_agg`` must not also hide pool status/health (the
    real risk scenario is a disk failing SMART in a pool that's still
    ``ONLINE`` — exactly when the operator most needs the rest of the
    Pools tab); a failing ``iscsi.*`` query must not also hide a working
    SMB/NFS listing; a failing ``vm.query`` must not also hide ``apps``
    that responded fine.
    """
    try:
        return fn(), None
    except TrueNASError as e:
        log.warning(f"[truenas] '{label}' failed, degrading gracefully: {e}")
        return default, str(e)


class ReadOnlySubsystem(Exception):
    """Raised by the default ``Subsystem.write()`` — every subsystem is
    read-only until its F2+ writer is implemented and wired behind the
    write-path guardrails (brief §5: dry-run, confirm, audit, verify)."""

    def __init__(self, subsystem_id):
        super().__init__(
            f"subsystem '{subsystem_id}' has no write support in this phase")


class ConfirmationRequired(Exception):
    """Raised by a destructive write's envelope builder (F2+) when the
    caller-supplied ``confirm_name`` doesn't match the resource's full
    name/id — the brief §5 step 2 GitHub-style typed-confirmation guard.

    Deliberately raised from inside the SAME builder function that also
    constructs the JSON-RPC envelope for both dry-run and real execution
    (never a separate check bolted on afterward) — this is what guarantees
    a delete can never be dry-run'd or executed without the confirmation
    having been validated first, in exactly one place.
    """

    def __init__(self, expected, got):
        super().__init__(
            f"confirmation mismatch: expected {expected!r}, got {got!r}")
        self.expected = expected
        self.got = got


@dataclass
class HealthReport:
    """Uniform health summary a subsystem can report for the Overview tab.

    ``details`` is a free-form dict — deliberately not over-specified, since
    what's useful to show differs per subsystem (pool health cares about
    unhealthy pool names; system health cares about alert counts).
    """
    healthy: bool
    summary: str
    details: dict = field(default_factory=dict)

    def to_dict(self):
        return {'healthy': self.healthy, 'summary': self.summary, 'details': self.details}


class Subsystem:
    """Base contract. Concrete subsystems override ``list``/``read``/
    ``health`` as they make sense for that TrueNAS concept — not every
    subsystem has a meaningful single-object ``read`` (e.g. pools' natural
    unit IS the list), so overriding is opt-in, not enforced via ABC
    machinery that would just add ceremony for no real safety benefit here.
    """
    SUBSYSTEM_ID = None

    def list(self, conn):
        raise NotImplementedError(f'{self.SUBSYSTEM_ID}.list() not implemented')

    def read(self, conn, id):
        raise NotImplementedError(f'{self.SUBSYSTEM_ID}.read() not implemented')

    def health(self, conn):
        raise NotImplementedError(f'{self.SUBSYSTEM_ID}.health() not implemented')

    def write(self, conn, op, payload):
        raise ReadOnlySubsystem(self.SUBSYSTEM_ID)
