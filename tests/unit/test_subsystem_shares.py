# -*- coding: utf-8 -*-
"""shares subsystem: SMB/NFS/iSCSI, read-only in F1. ``list()`` returns a
dict keyed by kind (deliberate deviation from the generic list[dict] shape
— see module docstring)."""

from subsystems import shares
from tests.unit.fakes import FakeConn


def _responses(**overrides):
    base = {
        'sharing.smb.query': [{'name': 'smb1'}],
        'sharing.nfs.query': [{'paths': ['/mnt/tank/nfs1']}],
        'iscsi.target.query': [{'name': 'target1'}],
        'iscsi.extent.query': [{'name': 'extent1'}],
        'iscsi.targetextent.query': [{'id': 1}],
    }
    base.update(overrides)
    return base


def test_list_calls_all_five_share_collections():
    conn = FakeConn(_responses())
    result = shares.shares.list(conn)
    assert result == {
        'smb': [{'name': 'smb1'}],
        'nfs': [{'paths': ['/mnt/tank/nfs1']}],
        'iscsi_targets': [{'name': 'target1'}],
        'iscsi_extents': [{'name': 'extent1'}],
        'iscsi_targetextents': [{'id': 1}],
    }
    assert set(conn.methods_called()) == {
        'sharing.smb.query', 'sharing.nfs.query', 'iscsi.target.query',
        'iscsi.extent.query', 'iscsi.targetextent.query',
    }


def test_individual_helpers_handle_none_response():
    conn = FakeConn({
        'sharing.smb.query': None, 'sharing.nfs.query': None,
        'iscsi.target.query': None, 'iscsi.extent.query': None,
        'iscsi.targetextent.query': None,
    })
    assert shares.list_smb(conn) == []
    assert shares.list_nfs(conn) == []
    assert shares.list_iscsi_targets(conn) == []
    assert shares.list_iscsi_extents(conn) == []
    assert shares.list_iscsi_targetextents(conn) == []
