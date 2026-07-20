# -*- coding: utf-8 -*-
"""replication subsystem: replication.query, read-only in F1 (no task
creation — that's F4, once a second real instance exists to replicate to)."""

from subsystems import replication
from tests.unit.fakes import FakeConn


def test_list_replications_calls_replication_query():
    conn = FakeConn({'replication.query': [{'id': 1, 'name': 'backup-to-253'}]})
    result = replication.list_replications(conn)
    assert result == [{'id': 1, 'name': 'backup-to-253'}]
    assert conn.methods_called() == ['replication.query']


def test_list_replications_handles_none_response():
    conn = FakeConn({'replication.query': None})
    assert replication.list_replications(conn) == []


def test_read_finds_replication_by_name():
    conn = FakeConn({'replication.query': [{'id': 1, 'name': 'backup-to-253'}]})
    assert replication.replication.read(conn, 'backup-to-253')['id'] == 1


def test_read_returns_none_for_unknown_replication():
    conn = FakeConn({'replication.query': [{'id': 1, 'name': 'backup-to-253'}]})
    assert replication.replication.read(conn, 'ghost') is None


def test_list_via_subsystem_instance():
    conn = FakeConn({'replication.query': [{'id': 1}]})
    assert replication.replication.list(conn) == [{'id': 1}]
