# -*- coding: utf-8 -*-
"""Datasets/zvols subsystem: ``pool.dataset.query`` + ``pool.dataset.get_quota``
(read, F1) and ``pool.dataset.create``/``update``/``delete`` (write, F2).

``list_datasets()``/``DatasetsSubsystem.list()`` is what the Datasets tab's
read fetch calls — ``quota()`` below is a standalone, NOT YET WIRED helper
for a future per-dataset quota display: no route or UI code calls it yet,
only this module's own tests do. It's kept here (rather than deferred
entirely to a later phase) because ``pool.dataset.get_quota``'s per-dataset
params (``dataset_id``, quota type) needed *some* defensive shape decided
now, so a future caller building a "quota per dataset" sweep doesn't have
to rediscover the failure-isolation requirement. When that sweep IS built,
call ``quota()`` once per dataset and keep the same contract: a bad
dataset id degrades to ``[]`` for THAT dataset only, never aborts the rest
of the sweep — mirroring the ``safe_call`` isolation pattern used by
``shares``/``apps_vms``/the pools and system routes.

Write path (F2, brief §5): every mutating op is split into a
``build_<op>_envelope(...)`` function (pure — no ``conn``, just returns
``(method, params)`` or raises) and a real ``<op>(conn, ...)`` function
that calls the SAME builder before ever touching ``conn``. routes/api.py's
dry-run endpoint calls the builder directly; the execute endpoint calls
the real op function, which calls the identical builder internally — this
is what makes it structurally impossible for dry-run and execution to
describe different JSON-RPC calls.

Sync-vs-async uncertainty (documented, not resolved — no live access this
session): TrueNAS's ``@job``-decorated methods return an integer job id
over JSON-RPC instead of the final result; whether ``pool.dataset.create``/
``update``/``delete`` are synchronous or job-wrapped in 25.10.1 was NOT
confirmed. Designed for the conservative case: every write function
returns the RAW call result as-is (int job id or dict/bool), and
routes/api.py's execute handler treats an int result as "dispatched,
pending verification" rather than asserting success — see that module's
docstring for the full verify-then-report flow.
"""

import logging

from core.errors import TrueNASError
from core.subsystem import ConfirmationRequired, Subsystem

log = logging.getLogger('plugin.truenas.subsystems.datasets')

DEFAULT_QUOTA_TYPE = 'USER'


def list_datasets(conn):
    """``pool.dataset.query`` — every dataset/zvol, attrs passthrough."""
    return conn.call('pool.dataset.query') or []


def quota(conn, dataset_id, quota_type=DEFAULT_QUOTA_TYPE):
    """``pool.dataset.get_quota`` for one dataset. Returns ``[]`` (not
    raises) on any TrueNAS-side error — read-only monitoring must degrade
    per-dataset, not all-or-nothing. Logs the failure (dataset id + cause)
    so a bad id or a real appliance error leaves a trace once this is
    wired to a caller, instead of vanishing silently."""
    try:
        return conn.call('pool.dataset.get_quota', [dataset_id, quota_type]) or []
    except TrueNASError as e:
        log.warning(f"[truenas] quota lookup failed for dataset {dataset_id!r} "
                    f"(quota_type={quota_type!r}): {e}")
        return []


# ---------------------------------------------------------------------------
# Write path (F2) — envelope builders + real ops. See module docstring.
# ---------------------------------------------------------------------------

def build_create_envelope(payload):
    """``payload`` is passed through as-is to ``pool.dataset.create`` —
    must include at least ``name`` (the full dataset path, e.g.
    ``'tank/data'``). Not otherwise validated here: TrueNAS itself is the
    authority on which fields are valid for a given dataset ``type``
    (FILESYSTEM vs VOLUME), and duplicating that validation client-side
    would just drift out of sync with the real schema."""
    if not isinstance(payload, dict) or not payload.get('name'):
        raise ValueError("payload.name is required (the full dataset path)")
    return 'pool.dataset.create', [payload]


def build_update_envelope(dataset_id, payload):
    """``payload`` is the partial set of fields to change (e.g.
    ``{'volsize': bytes, 'force_size': True}`` for a zvol resize, or a
    quota field) — passed through as-is to ``pool.dataset.update``."""
    if not dataset_id:
        raise ValueError('dataset_id is required')
    if not isinstance(payload, dict) or not payload:
        raise ValueError('payload must be a non-empty object of fields to change')
    return 'pool.dataset.update', [dataset_id, payload]


def build_delete_envelope(dataset_id, confirm_name, options=None):
    """GitHub-style typed confirmation (brief §5 step 2): refuses to build
    the delete envelope — meaning dry-run ALSO refuses, since it calls this
    same function — unless ``confirm_name`` matches ``dataset_id`` exactly.
    ``options`` (e.g. ``{'recursive': True}``) is passed through as-is when
    given."""
    if not dataset_id:
        raise ValueError('dataset_id is required')
    if confirm_name != dataset_id:
        raise ConfirmationRequired(expected=dataset_id, got=confirm_name)
    params = [dataset_id, options] if options else [dataset_id]
    return 'pool.dataset.delete', params


def create(conn, payload):
    """``pool.dataset.create``. Returns the raw call result — may be the
    created dataset dict, ``True``, or an int job id depending on whether
    this method is job-wrapped in the target's TrueNAS version (not
    confirmed live this session — see module docstring)."""
    method, params = build_create_envelope(payload)
    return conn.call(method, params)


def update(conn, dataset_id, payload):
    """``pool.dataset.update``. Same result-shape caveat as ``create()``."""
    method, params = build_update_envelope(dataset_id, payload)
    return conn.call(method, params)


def delete(conn, dataset_id, confirm_name, options=None):
    """``pool.dataset.delete``. Raises ``ConfirmationRequired`` (via the
    builder) BEFORE any call reaches TrueNAS if ``confirm_name`` doesn't
    match ``dataset_id`` exactly."""
    method, params = build_delete_envelope(dataset_id, confirm_name, options)
    return conn.call(method, params)


class DatasetsSubsystem(Subsystem):
    SUBSYSTEM_ID = 'datasets'

    def list(self, conn):
        return list_datasets(conn)

    def read(self, conn, id):
        for ds in list_datasets(conn):
            if ds.get('id') == id or ds.get('name') == id:
                return ds
        return None


datasets = DatasetsSubsystem()
