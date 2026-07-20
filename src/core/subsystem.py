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

from dataclasses import dataclass, field


class ReadOnlySubsystem(Exception):
    """Raised by the default ``Subsystem.write()`` — every subsystem is
    read-only until its F2+ writer is implemented and wired behind the
    write-path guardrails (brief §5: dry-run, confirm, audit, verify)."""

    def __init__(self, subsystem_id):
        super().__init__(
            f"subsystem '{subsystem_id}' has no write support in this phase")


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
