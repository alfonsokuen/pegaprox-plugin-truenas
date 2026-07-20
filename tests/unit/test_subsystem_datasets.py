# -*- coding: utf-8 -*-
"""datasets subsystem: pool.dataset.query (list) + pool.dataset.get_quota
(best-effort per-dataset, never fails the whole sweep) — read, F1.
create/update/delete — write, F2. Fixture dataset names are fictitious
(``tank/test-dataset``), never a real pool/dataset name."""

import pytest

from core.errors import TrueNASRPCError
from core.subsystem import ConfirmationRequired
from core.ws_client import WRITE_TIMEOUT
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


# ---------------------------------------------------------------------------
# Write path (F2): envelope builders (pure, no conn) + real ops that call
# the SAME builder. Fixture names: tank/test-dataset (fictitious).
# ---------------------------------------------------------------------------

def test_build_create_envelope_passes_payload_through():
    payload = {'name': 'tank/test-dataset', 'type': 'FILESYSTEM'}
    method, params = datasets.build_create_envelope(payload)
    assert method == 'pool.dataset.create'
    assert params == [payload]


def test_build_create_envelope_requires_name():
    with pytest.raises(ValueError):
        datasets.build_create_envelope({'type': 'FILESYSTEM'})
    with pytest.raises(ValueError):
        datasets.build_create_envelope(None)


def test_create_calls_the_same_envelope_the_builder_produces():
    payload = {'name': 'tank/test-dataset'}
    conn = FakeConn({'pool.dataset.create': {'id': 'tank/test-dataset'}})
    result = datasets.create(conn, payload)
    assert result == {'id': 'tank/test-dataset'}
    method, params = conn.calls[0]
    assert (method, params) == datasets.build_create_envelope(payload)


def test_build_update_envelope_requires_dataset_id_and_nonempty_payload():
    with pytest.raises(ValueError):
        datasets.build_update_envelope(None, {'volsize': 100})
    with pytest.raises(ValueError):
        datasets.build_update_envelope('tank/test-dataset', {})
    with pytest.raises(ValueError):
        datasets.build_update_envelope('tank/test-dataset', None)


def test_build_update_envelope_shape():
    method, params = datasets.build_update_envelope(
        'tank/test-dataset', {'volsize': 2048, 'force_size': True})
    assert method == 'pool.dataset.update'
    assert params == ['tank/test-dataset', {'volsize': 2048, 'force_size': True}]


def test_update_calls_the_same_envelope_the_builder_produces():
    conn = FakeConn({'pool.dataset.update': True})
    changes = {'volsize': 4096, 'force_size': True}
    result = datasets.update(conn, 'tank/test-dataset', changes)
    assert result is True
    method, params = conn.calls[0]
    assert (method, params) == datasets.build_update_envelope('tank/test-dataset', changes)


def test_build_delete_envelope_requires_dataset_id():
    with pytest.raises(ValueError):
        datasets.build_delete_envelope(None, None)


def test_build_delete_envelope_rejects_confirmation_mismatch():
    """GitHub-style typed confirmation (brief §5 step 2) — refuses to build
    the envelope at all, before any TrueNAS call, if confirm_name doesn't
    match the dataset id exactly."""
    with pytest.raises(ConfirmationRequired) as exc_info:
        datasets.build_delete_envelope('tank/test-dataset', 'tank/wrong-name')
    assert 'tank/test-dataset' in str(exc_info.value)


def test_build_delete_envelope_accepts_exact_confirmation():
    method, params = datasets.build_delete_envelope('tank/test-dataset', 'tank/test-dataset')
    assert method == 'pool.dataset.delete'
    assert params == ['tank/test-dataset']


def test_build_delete_envelope_includes_options_when_given():
    method, params = datasets.build_delete_envelope(
        'tank/test-dataset', 'tank/test-dataset', options={'recursive': True})
    assert params == ['tank/test-dataset', {'recursive': True}]


def test_delete_refuses_to_call_truenas_on_confirmation_mismatch():
    """The core safety guarantee: a wrong confirm_name must never reach
    conn.call() at all — not even attempted."""
    conn = FakeConn({})  # would raise AssertionError if call() were reached
    with pytest.raises(ConfirmationRequired):
        datasets.delete(conn, 'tank/test-dataset', 'tank/wrong-name')
    assert conn.calls == []


def test_delete_calls_the_same_envelope_the_builder_produces():
    conn = FakeConn({'pool.dataset.delete': True})
    result = datasets.delete(conn, 'tank/test-dataset', 'tank/test-dataset')
    assert result is True
    method, params = conn.calls[0]
    assert (method, params) == datasets.build_delete_envelope(
        'tank/test-dataset', 'tank/test-dataset')


# ---------------------------------------------------------------------------
# Regression (QA fable, 2026-07-20, pre real-.64 test): create/update/delete
# must use WRITE_TIMEOUT, not the 10s read default — a slow real ZFS write
# (recursive delete, encrypted/dedup create) must not be misreported as a
# TrueNASTimeoutError while it's still genuinely in flight on TrueNAS.
# ---------------------------------------------------------------------------

def test_create_uses_write_timeout_not_default():
    conn = FakeConn({'pool.dataset.create': {'id': 'tank/test-dataset'}})
    datasets.create(conn, {'name': 'tank/test-dataset'})
    assert conn.timeouts[0] == WRITE_TIMEOUT


def test_update_uses_write_timeout_not_default():
    conn = FakeConn({'pool.dataset.update': True})
    datasets.update(conn, 'tank/test-dataset', {'volsize': 4096})
    assert conn.timeouts[0] == WRITE_TIMEOUT


def test_delete_uses_write_timeout_not_default():
    conn = FakeConn({'pool.dataset.delete': True})
    datasets.delete(conn, 'tank/test-dataset', 'tank/test-dataset')
    assert conn.timeouts[0] == WRITE_TIMEOUT
