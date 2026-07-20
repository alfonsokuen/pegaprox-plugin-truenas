# -*- coding: utf-8 -*-
"""snapshots subsystem: pool.snapshot.query + pool.snapshottask.query
(read, F1); create/delete (write, F2). Fixture names are fictitious
(``tank/test-dataset``), never a real pool/dataset name."""

import pytest

from core.subsystem import ConfirmationRequired
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


# ---------------------------------------------------------------------------
# Write path (F2): envelope builders (pure, no conn) + real ops that call
# the SAME builder.
# ---------------------------------------------------------------------------

def test_build_create_envelope_shape():
    method, params = snapshots.build_create_envelope('tank/test-dataset', 'snap1')
    assert method == 'pool.snapshot.create'
    assert params == [{'dataset': 'tank/test-dataset', 'name': 'snap1', 'recursive': False}]


def test_build_create_envelope_recursive_flag():
    method, params = snapshots.build_create_envelope('tank/test-dataset', 'snap1', recursive=True)
    assert params == [{'dataset': 'tank/test-dataset', 'name': 'snap1', 'recursive': True}]


def test_build_create_envelope_requires_dataset_and_name():
    with pytest.raises(ValueError):
        snapshots.build_create_envelope(None, 'snap1')
    with pytest.raises(ValueError):
        snapshots.build_create_envelope('tank/test-dataset', None)


def test_create_calls_the_same_envelope_the_builder_produces():
    conn = FakeConn({'pool.snapshot.create': {'id': 'tank/test-dataset@snap1'}})
    result = snapshots.create(conn, 'tank/test-dataset', 'snap1')
    assert result == {'id': 'tank/test-dataset@snap1'}
    method, params = conn.calls[0]
    assert (method, params) == snapshots.build_create_envelope('tank/test-dataset', 'snap1')


def test_build_delete_envelope_requires_snapshot_id():
    with pytest.raises(ValueError):
        snapshots.build_delete_envelope(None, None)


def test_build_delete_envelope_rejects_confirmation_mismatch():
    with pytest.raises(ConfirmationRequired) as exc_info:
        snapshots.build_delete_envelope('tank/test-dataset@snap1', 'wrong-name')
    assert 'tank/test-dataset@snap1' in str(exc_info.value)


def test_build_delete_envelope_accepts_exact_confirmation():
    method, params = snapshots.build_delete_envelope(
        'tank/test-dataset@snap1', 'tank/test-dataset@snap1')
    assert method == 'pool.snapshot.delete'
    assert params == ['tank/test-dataset@snap1']


def test_delete_refuses_to_call_truenas_on_confirmation_mismatch():
    conn = FakeConn({})  # would raise AssertionError if call() were reached
    with pytest.raises(ConfirmationRequired):
        snapshots.delete(conn, 'tank/test-dataset@snap1', 'wrong-name')
    assert conn.calls == []


def test_delete_calls_the_same_envelope_the_builder_produces():
    conn = FakeConn({'pool.snapshot.delete': True})
    result = snapshots.delete(conn, 'tank/test-dataset@snap1', 'tank/test-dataset@snap1')
    assert result is True
    method, params = conn.calls[0]
    assert (method, params) == snapshots.build_delete_envelope(
        'tank/test-dataset@snap1', 'tank/test-dataset@snap1')
