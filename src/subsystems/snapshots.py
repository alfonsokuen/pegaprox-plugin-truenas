# -*- coding: utf-8 -*-
"""Snapshots subsystem: ``pool.snapshot.query`` (existing snapshots) +
``pool.snapshottask.query`` (periodic snapshot task schedule) — read, F1.
``pool.snapshot.create``/``delete`` — write, F2.

CRUD on snapshot TASKS (the periodic schedule, not one-off snapshots) is
still out of scope — only one-off create/delete of an actual snapshot is
implemented here, per this phase's exact ask.

Same builder/op split and sync-vs-async caveat as ``datasets.py`` — see
that module's docstring for the shared rationale (not repeated here).
"""

from core.subsystem import ConfirmationRequired, Subsystem

DEFAULT_RECURSIVE = False


def list_snapshots(conn):
    """``pool.snapshot.query`` — every existing snapshot, attrs passthrough."""
    return conn.call('pool.snapshot.query') or []


def list_tasks(conn):
    """``pool.snapshottask.query`` — periodic snapshot task definitions."""
    return conn.call('pool.snapshottask.query') or []


# ---------------------------------------------------------------------------
# Write path (F2) — envelope builders + real ops.
# ---------------------------------------------------------------------------

def build_create_envelope(dataset, name, recursive=DEFAULT_RECURSIVE):
    if not dataset:
        raise ValueError('dataset is required')
    if not name:
        raise ValueError('name is required')
    payload = {'dataset': dataset, 'name': name, 'recursive': bool(recursive)}
    return 'pool.snapshot.create', [payload]


def build_delete_envelope(snapshot_id, confirm_name):
    """GitHub-style typed confirmation (brief §5 step 2), same pattern as
    ``datasets.build_delete_envelope`` — ``confirm_name`` must match
    ``snapshot_id`` (the full ``dataset@name`` snapshot identifier)
    exactly, checked BEFORE building the envelope dry-run/execute share."""
    if not snapshot_id:
        raise ValueError('snapshot_id is required')
    if confirm_name != snapshot_id:
        raise ConfirmationRequired(expected=snapshot_id, got=confirm_name)
    return 'pool.snapshot.delete', [snapshot_id]


def create(conn, dataset, name, recursive=DEFAULT_RECURSIVE):
    """``pool.snapshot.create``. Returns the raw call result (dict, bool,
    or int job id — see datasets.py's module docstring for the
    sync-vs-async caveat, identical here)."""
    method, params = build_create_envelope(dataset, name, recursive)
    return conn.call(method, params)


def delete(conn, snapshot_id, confirm_name):
    """``pool.snapshot.delete``. Raises ``ConfirmationRequired`` (via the
    builder) BEFORE any call reaches TrueNAS if ``confirm_name`` doesn't
    match ``snapshot_id`` exactly."""
    method, params = build_delete_envelope(snapshot_id, confirm_name)
    return conn.call(method, params)


class SnapshotsSubsystem(Subsystem):
    SUBSYSTEM_ID = 'snapshots'

    def list(self, conn):
        return list_snapshots(conn)

    def read(self, conn, id):
        for snap in list_snapshots(conn):
            if snap.get('id') == id or snap.get('name') == id:
                return snap
        return None


snapshots = SnapshotsSubsystem()
