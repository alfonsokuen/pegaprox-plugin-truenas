# -*- coding: utf-8 -*-
"""snapshots subsystem: pool.snapshot.query + pool.snapshottask.query,
read-only in F1."""

from subsystems import snapshots
from tests.unit.fakes import FakeConn


def test_list_snapshots_calls_pool_snapshot_query():
    conn = FakeConn({'pool.snapshot.query': [{'id': 'tank@auto-1', 'name': 'tank@auto-1'}]})
    result = snapshots.list_snapshots(conn)
    assert result == [{'id': 'tank@auto-1', 'name': 'tank@auto-1'}]
    assert conn.methods_called() == ['pool.snapshot.query']


def test_list_snapshots_handles_none_response():
    conn = FakeConn({'pool.snapshot.query': None})
    assert snapshots.list_snapshots(conn) == []


def test_list_tasks_calls_pool_snapshottask_query():
    conn = FakeConn({'pool.snapshottask.query': [{'id': 1, 'dataset': 'tank/data'}]})
    result = snapshots.list_tasks(conn)
    assert result == [{'id': 1, 'dataset': 'tank/data'}]
    assert conn.methods_called() == ['pool.snapshottask.query']


def test_read_finds_snapshot_by_name():
    conn = FakeConn({'pool.snapshot.query': [{'id': 's1', 'name': 'tank@auto-1'}]})
    assert snapshots.snapshots.read(conn, 'tank@auto-1')['id'] == 's1'


def test_read_returns_none_for_unknown_snapshot():
    conn = FakeConn({'pool.snapshot.query': [{'id': 's1', 'name': 'tank@auto-1'}]})
    assert snapshots.snapshots.read(conn, 'ghost') is None


def test_list_via_subsystem_instance():
    conn = FakeConn({'pool.snapshot.query': [{'id': 's1'}]})
    assert snapshots.snapshots.list(conn) == [{'id': 's1'}]
