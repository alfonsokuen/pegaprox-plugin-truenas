# -*- coding: utf-8 -*-
"""Snapshots subsystem: ``pool.snapshot.query`` (existing snapshots) +
``pool.snapshottask.query`` (periodic snapshot task schedule).

Read-only in F1 — create/delete snapshot, CRUD on snapshot tasks, are F2.
"""

from core.subsystem import Subsystem


def list_snapshots(conn):
    """``pool.snapshot.query`` — every existing snapshot, attrs passthrough."""
    return conn.call('pool.snapshot.query') or []


def list_tasks(conn):
    """``pool.snapshottask.query`` — periodic snapshot task definitions."""
    return conn.call('pool.snapshottask.query') or []


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
