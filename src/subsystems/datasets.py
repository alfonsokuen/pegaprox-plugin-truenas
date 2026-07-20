# -*- coding: utf-8 -*-
"""Datasets/zvols subsystem: ``pool.dataset.query`` + ``pool.dataset.get_quota``.

Read-only in F1 — create/update/delete/resize are F2, gated behind the
dry-run/confirm/audit write-path (brief §5) once a dedicated test dataset
on a healthy pool is designated for it.

``get_quota`` requires per-dataset params (``dataset_id``, quota type) not
captured in a live fixture this session — implemented defensively: any
``TrueNASError`` from a single dataset's quota lookup is caught and turned
into an empty list for THAT dataset rather than failing the whole
``list_with_quotas`` sweep (so one dataset with an unexpected id format
doesn't take down the entire Datasets tab).
"""

from core.errors import TrueNASError
from core.subsystem import Subsystem

DEFAULT_QUOTA_TYPE = 'USER'


def list_datasets(conn):
    """``pool.dataset.query`` — every dataset/zvol, attrs passthrough."""
    return conn.call('pool.dataset.query') or []


def quota(conn, dataset_id, quota_type=DEFAULT_QUOTA_TYPE):
    """``pool.dataset.get_quota`` for one dataset. Returns ``[]`` (not
    raises) on any TrueNAS-side error — read-only monitoring must degrade
    per-dataset, not all-or-nothing."""
    try:
        return conn.call('pool.dataset.get_quota', [dataset_id, quota_type]) or []
    except TrueNASError:
        return []


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
