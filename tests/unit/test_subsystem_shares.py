# -*- coding: utf-8 -*-
"""shares subsystem: SMB/NFS/iSCSI, read-only in F1. ``list()`` returns a
dict keyed by kind (deliberate deviation from the generic list[dict] shape
— see module docstring). Each of the five collections is fetched
independently (safe_call) so one failing kind never hides the others."""

import pytest

from core.errors import TrueNASConnectionError
from core.subsystem import ConfirmationRequired
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
        'smb': [{'name': 'smb1'}], 'smb_error': None,
        'nfs': [{'paths': ['/mnt/tank/nfs1']}], 'nfs_error': None,
        'iscsi_targets': [{'name': 'target1'}], 'iscsi_targets_error': None,
        'iscsi_extents': [{'name': 'extent1'}], 'iscsi_extents_error': None,
        'iscsi_targetextents': [{'id': 1}], 'iscsi_targetextents_error': None,
    }
    assert set(conn.methods_called()) == {
        'sharing.smb.query', 'sharing.nfs.query', 'iscsi.target.query',
        'iscsi.extent.query', 'iscsi.targetextent.query',
    }


def test_failing_iscsi_query_does_not_hide_working_smb_and_nfs():
    conn = FakeConn(_responses(**{
        'iscsi.target.query': TrueNASConnectionError('iscsi subsystem timeout'),
    }))
    result = shares.shares.list(conn)
    assert result['smb'] == [{'name': 'smb1'}]
    assert result['smb_error'] is None
    assert result['nfs'] == [{'paths': ['/mnt/tank/nfs1']}]
    assert result['iscsi_targets'] == []
    assert 'iscsi subsystem timeout' in result['iscsi_targets_error']
    # The rest of iSCSI (extents/targetextents) is a SEPARATE call — still
    # attempted and still succeeds independently.
    assert result['iscsi_extents'] == [{'name': 'extent1'}]


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


# ---------------------------------------------------------------------------
# F4c: SMB/NFS create/update/delete. Verified live against real, actively-
# used shares on .64 (a real "nextcloud" SMB share, a real "PBS_NFS" NFS
# share backing Proxmox Backup Server) that both are synchronous — no
# write was executed against either real share, only schemas inspected.
# ---------------------------------------------------------------------------


def test_build_smb_create_envelope_requires_name_and_path():
    method, params = shares.build_smb_create_envelope({'name': 'docs', 'path': '/mnt/tank/docs'})
    assert method == 'sharing.smb.create'
    assert params == [{'name': 'docs', 'path': '/mnt/tank/docs'}]


def test_build_smb_create_envelope_rejects_missing_name():
    with pytest.raises(ValueError):
        shares.build_smb_create_envelope({'path': '/mnt/tank/docs'})


def test_build_smb_create_envelope_rejects_missing_path():
    with pytest.raises(ValueError):
        shares.build_smb_create_envelope({'name': 'docs'})


def test_build_smb_update_envelope():
    method, params = shares.build_smb_update_envelope(12, {'comment': 'updated'})
    assert method == 'sharing.smb.update'
    assert params == [12, {'comment': 'updated'}]


def test_build_smb_delete_envelope_requires_matching_confirmation():
    with pytest.raises(ConfirmationRequired):
        shares.build_smb_delete_envelope(12, 'wrong-name', 'nextcloud')


def test_build_smb_delete_envelope_succeeds_with_matching_confirmation():
    method, params = shares.build_smb_delete_envelope(12, 'nextcloud', 'nextcloud')
    assert method == 'sharing.smb.delete'
    assert params == [12]


def test_smb_delete_never_calls_truenas_without_confirmation():
    conn = FakeConn({})  # would raise if sharing.smb.delete were ever called
    with pytest.raises(ConfirmationRequired):
        shares.smb_delete(conn, 12, 'wrong', 'nextcloud')
    assert conn.methods_called() == []


def test_find_smb_by_id():
    conn = FakeConn({'sharing.smb.query': [{'id': 1, 'name': 'a'}, {'id': 12, 'name': 'nextcloud'}]})
    found = shares.find_smb(conn, 12)
    assert found['name'] == 'nextcloud'


def test_build_nfs_create_envelope_requires_path():
    with pytest.raises(ValueError):
        shares.build_nfs_create_envelope({})


def test_build_nfs_create_envelope():
    method, params = shares.build_nfs_create_envelope({'path': '/mnt/tank/nfs2'})
    assert method == 'sharing.nfs.create'
    assert params == [{'path': '/mnt/tank/nfs2'}]


def test_build_nfs_delete_envelope_confirms_against_path_not_a_name():
    """NFS shares have no 'name' field — confirmation is against path."""
    with pytest.raises(ConfirmationRequired):
        shares.build_nfs_delete_envelope(2, 'wrong-path', '/mnt/Backup_Proxmox/PBS')
    method, params = shares.build_nfs_delete_envelope(
        2, '/mnt/Backup_Proxmox/PBS', '/mnt/Backup_Proxmox/PBS')
    assert method == 'sharing.nfs.delete'
    assert params == [2]


def test_find_nfs_by_id():
    conn = FakeConn({'sharing.nfs.query': [{'id': 2, 'path': '/mnt/Backup_Proxmox/PBS'}]})
    found = shares.find_nfs(conn, 2)
    assert found['path'] == '/mnt/Backup_Proxmox/PBS'
