# -*- coding: utf-8 -*-
"""Shares subsystem: SMB, NFS and iSCSI (target/extent/targetextent),
read-only in F1. F4c adds create/update/delete for SMB and NFS ONLY —
iSCSI's write payload is a 3-way join (target + extent + targetextent) and
deliberately deferred rather than forced into this pass; SMB/NFS covers the
actual "share a folder without opening TrueNAS" ask.

F4c write path (brief §5, same shape as datasets/snapshots): every
mutating op is a pure ``build_<op>_envelope(...)`` + a real ``<op>(conn,
...)`` that calls the same builder first. Delete's typed-confirmation
guard has one real difference from datasets: a dataset's own ``id`` IS a
human-readable path (e.g. ``'tank/data'``), so its confirm field compares
directly against ``dataset_id``. An SMB/NFS share's ``id`` is an opaque
integer TrueNAS assigns — the builder has no ``conn`` to look up the
share's real name/path to confirm against, so the caller (routes/api.py)
passes the human label it already has from the UI row (``expected_name``,
the share's ``name``/``path`` at the moment the delete button was clicked)
alongside the typed ``confirm_name``, and the builder compares those two
directly — never trusting the client to have gotten ``expected_name``
right, since a wrong `expected_name` still can't produce a match unless
the operator's typed input coincidentally equals it.

Verified live 2026-07-20 against real, ACTIVELY-USED shares on `.64`
(a real "nextcloud" SMB share and a real "PBS_NFS" NFS share backing
Proxmox Backup Server) that ``sharing.smb.create/update/delete`` and
``sharing.nfs.create/update/delete`` are all synchronous (``job: False``)
— no job-id/pending handling needed, unlike VM/App writes. Schemas
verified via ``core.get_methods`` only; no write was executed against
either of those real, in-use shares.

Deliberate deviation from the generic ``list(conn) -> list[dict]`` shape:
"shares" isn't one TrueNAS collection, it's five (``sharing.smb.query``,
``sharing.nfs.query``, three ``iscsi.*.query`` calls), and the UI's own
layout (brief §6) already wants them as separate SMB/NFS/iSCSI tabs, not
one flattened list an operator would have to filter client-side. ``list()``
therefore returns a dict keyed by kind — documented here rather than forced
into a shape that would just be un-flattened again by the caller.

The iSCSI write payload is explicitly unconfirmed per the brief (§4.2) —
irrelevant here since F1 only reads; ``sharing.smb.query``/
``sharing.nfs.query``/``iscsi.*.query`` take no required params for a full
listing.

Each of the five collections is fetched independently via ``safe_call`` —
a failing ``iscsi.*`` query (silent-failure-hunter finding, F1 review round
2) must not also take down SMB/NFS results that DID come back fine. Every
kind gets a ``<kind>_error`` key alongside it (``None`` on success).
"""

from core.subsystem import ConfirmationRequired, Subsystem, parallel_safe_calls
from core.ws_client import WRITE_TIMEOUT


def list_smb(conn):
    return conn.call('sharing.smb.query') or []


def list_nfs(conn):
    return conn.call('sharing.nfs.query') or []


def list_iscsi_targets(conn):
    return conn.call('iscsi.target.query') or []


def list_iscsi_extents(conn):
    return conn.call('iscsi.extent.query') or []


def list_iscsi_targetextents(conn):
    return conn.call('iscsi.targetextent.query') or []


class SharesSubsystem(Subsystem):
    SUBSYSTEM_ID = 'shares'

    def list(self, conn):
        """Returns a dict, not a flat list — see module docstring. Each of
        the five collections degrades independently AND is fetched
        CONCURRENTLY (``parallel_safe_calls`` — perf finding 2026-07-21:
        five sequential round-trips for five independent reads was the
        single biggest contributor to a slow Shares tab). One failing
        kind still never hides the others."""
        (smb, smb_error), (nfs, nfs_error), \
            (targets, targets_error), (extents, extents_error), \
            (targetextents, targetextents_error) = parallel_safe_calls([
                ('sharing.smb.query', lambda: list_smb(conn), []),
                ('sharing.nfs.query', lambda: list_nfs(conn), []),
                ('iscsi.target.query', lambda: list_iscsi_targets(conn), []),
                ('iscsi.extent.query', lambda: list_iscsi_extents(conn), []),
                ('iscsi.targetextent.query', lambda: list_iscsi_targetextents(conn), []),
            ])
        return {
            'smb': smb, 'smb_error': smb_error,
            'nfs': nfs, 'nfs_error': nfs_error,
            'iscsi_targets': targets, 'iscsi_targets_error': targets_error,
            'iscsi_extents': extents, 'iscsi_extents_error': extents_error,
            'iscsi_targetextents': targetextents, 'iscsi_targetextents_error': targetextents_error,
        }


shares = SharesSubsystem()


def build_smb_create_envelope(payload):
    """``name`` and ``path`` are the only TrueNAS-required fields
    (confirmed live via ``sharing.smb.create``'s schema) — everything else
    in ``payload`` (purpose/comment/readonly/browsable/...) is passed
    through as-is, TrueNAS is the authority on the rest."""
    if not isinstance(payload, dict) or not payload.get('name') or not payload.get('path'):
        raise ValueError('payload.name and payload.path are required')
    return 'sharing.smb.create', [payload]


def build_smb_update_envelope(share_id, payload):
    if not share_id:
        raise ValueError('share_id is required')
    if not isinstance(payload, dict) or not payload:
        raise ValueError('payload must be a non-empty object of fields to change')
    return 'sharing.smb.update', [share_id, payload]


def build_smb_delete_envelope(share_id, confirm_name, expected_name):
    """See module docstring: ``expected_name`` is the share's real ``name``
    as already known to the caller from the UI row, not looked up here —
    this builder never touches ``conn``."""
    if not share_id:
        raise ValueError('share_id is required')
    if not expected_name:
        raise ValueError('expected_name is required')
    if confirm_name != expected_name:
        raise ConfirmationRequired(expected=expected_name, got=confirm_name)
    return 'sharing.smb.delete', [share_id]


def smb_create(conn, payload):
    method, params = build_smb_create_envelope(payload)
    return conn.call(method, params, timeout=WRITE_TIMEOUT)


def smb_update(conn, share_id, payload):
    method, params = build_smb_update_envelope(share_id, payload)
    return conn.call(method, params, timeout=WRITE_TIMEOUT)


def smb_delete(conn, share_id, confirm_name, expected_name):
    method, params = build_smb_delete_envelope(share_id, confirm_name, expected_name)
    return conn.call(method, params, timeout=WRITE_TIMEOUT)


def build_nfs_create_envelope(payload):
    """Only ``path`` is TrueNAS-required (confirmed live). NFS has no
    ``name`` field — shares are identified by ``path``/``comment``."""
    if not isinstance(payload, dict) or not payload.get('path'):
        raise ValueError('payload.path is required')
    return 'sharing.nfs.create', [payload]


def build_nfs_update_envelope(share_id, payload):
    if not share_id:
        raise ValueError('share_id is required')
    if not isinstance(payload, dict) or not payload:
        raise ValueError('payload must be a non-empty object of fields to change')
    return 'sharing.nfs.update', [share_id, payload]


def build_nfs_delete_envelope(share_id, confirm_name, expected_path):
    """``expected_path`` is the share's real ``path`` as already known to
    the caller — NFS shares have no ``name`` field to confirm against."""
    if not share_id:
        raise ValueError('share_id is required')
    if not expected_path:
        raise ValueError('expected_path is required')
    if confirm_name != expected_path:
        raise ConfirmationRequired(expected=expected_path, got=confirm_name)
    return 'sharing.nfs.delete', [share_id]


def nfs_create(conn, payload):
    method, params = build_nfs_create_envelope(payload)
    return conn.call(method, params, timeout=WRITE_TIMEOUT)


def nfs_update(conn, share_id, payload):
    method, params = build_nfs_update_envelope(share_id, payload)
    return conn.call(method, params, timeout=WRITE_TIMEOUT)


def nfs_delete(conn, share_id, confirm_name, expected_path):
    method, params = build_nfs_delete_envelope(share_id, confirm_name, expected_path)
    return conn.call(method, params, timeout=WRITE_TIMEOUT)


def find_smb(conn, share_id):
    for share in list_smb(conn):
        if str(share.get('id')) == str(share_id):
            return share
    return None


def find_nfs(conn, share_id):
    for share in list_nfs(conn):
        if str(share.get('id')) == str(share_id):
            return share
    return None
