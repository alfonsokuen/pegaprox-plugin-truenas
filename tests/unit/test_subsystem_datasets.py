# -*- coding: utf-8 -*-
"""datasets subsystem: pool.dataset.query (list) + pool.dataset.get_quota
(best-effort per-dataset, never fails the whole sweep)."""

from core.errors import TrueNASRPCError
from subsystems import datasets
from tests.unit.fakes import FakeConn


def test_list_datasets_calls_pool_dataset_query():
    conn = FakeConn({'pool.dataset.query': [{'id': 'tank/data', 'name': 'tank/data'}]})
    result = datasets.list_datasets(conn)
    assert result == [{'id': 'tank/data', 'name': 'tank/data'}]
    assert conn.methods_called() == ['pool.dataset.query']


def test_list_datasets_handles_none_response():
    conn = FakeConn({'pool.dataset.query': None})
    assert datasets.list_datasets(conn) == []


def test_quota_returns_result():
    conn = FakeConn({'pool.dataset.get_quota': [{'name': 'user1', 'quota': 1000}]})
    result = datasets.quota(conn, 'tank/data', 'USER')
    assert result == [{'name': 'user1', 'quota': 1000}]
    method, params = conn.calls[0]
    assert method == 'pool.dataset.get_quota'
    assert params == ['tank/data', 'USER']


def test_quota_swallows_truenas_error_and_returns_empty_list():
    conn = FakeConn({'pool.dataset.get_quota': TrueNASRPCError('pool.dataset.get_quota', {'message': 'bad id'})})
    result = datasets.quota(conn, 'not-a-real-dataset')
    assert result == []


def test_read_finds_dataset_by_id():
    conn = FakeConn({'pool.dataset.query': [{'id': 'tank/data'}]})
    assert datasets.datasets.read(conn, 'tank/data') == {'id': 'tank/data'}


def test_read_returns_none_for_unknown_dataset():
    conn = FakeConn({'pool.dataset.query': [{'id': 'tank/data'}]})
    assert datasets.datasets.read(conn, 'ghost') is None


def test_list_via_subsystem_instance():
    conn = FakeConn({'pool.dataset.query': [{'id': 'tank/data'}]})
    assert datasets.datasets.list(conn) == [{'id': 'tank/data'}]
