# -*- coding: utf-8 -*-
"""Replication subsystem: ``replication.query`` — state/last-run of
configured replication tasks. Read-only in F1; creating tasks (which
reference a ``keychaincredential`` id for the remote, per the brief §4.2)
is F4, once a second real instance (e.g. `.253`) exists to replicate to.
"""

from core.subsystem import Subsystem


def list_replications(conn):
    return conn.call('replication.query') or []


class ReplicationSubsystem(Subsystem):
    SUBSYSTEM_ID = 'replication'

    def list(self, conn):
        return list_replications(conn)

    def read(self, conn, id):
        for repl in list_replications(conn):
            if repl.get('id') == id or repl.get('name') == id:
                return repl
        return None


replication = ReplicationSubsystem()
