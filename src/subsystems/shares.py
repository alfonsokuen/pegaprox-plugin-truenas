# -*- coding: utf-8 -*-
"""Shares subsystem: SMB, NFS and iSCSI (target/extent/targetextent),
read-only in F1 — CRUD lands in F3 per the brief's phase table.

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
"""

from core.subsystem import Subsystem


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
        """Returns a dict, not a flat list — see module docstring."""
        return {
            'smb': list_smb(conn),
            'nfs': list_nfs(conn),
            'iscsi_targets': list_iscsi_targets(conn),
            'iscsi_extents': list_iscsi_extents(conn),
            'iscsi_targetextents': list_iscsi_targetextents(conn),
        }


shares = SharesSubsystem()
