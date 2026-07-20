# -*- coding: utf-8 -*-
"""pools subsystem: pool.query/disk.query/disk.temperature_agg, topology
walk for degraded-pool disk exclusion (brief §4.3/§9 safety correction —
SMART queries against a failing/resilvering disk can add real load;
pool.query/scan reading pure ZFS kernel state cannot)."""

from subsystems import pools
from tests.unit.fakes import FakeConn


def _pool(name='tank', status='ONLINE', healthy=True, topology=None, scan=None):
    return {
        'name': name, 'status': status, 'healthy': healthy,
        'topology': topology or {}, 'scan': scan or {'state': 'FINISHED'},
    }


def _leaf(disk):
    return {'disk': disk, 'type': 'DISK'}


def _mirror(*disks):
    return {'type': 'MIRROR', 'children': [_leaf(d) for d in disks]}


def test_list_pools_calls_pool_query():
    conn = FakeConn({'pool.query': [_pool()]})
    assert pools.list_pools(conn) == [_pool()]
    assert conn.methods_called() == ['pool.query']


def test_list_pools_handles_none_response():
    conn = FakeConn({'pool.query': None})
    assert pools.list_pools(conn) == []


def test_list_disks_calls_disk_query():
    conn = FakeConn({'disk.query': [{'name': 'sda'}]})
    assert pools.list_disks(conn) == [{'name': 'sda'}]


def test_walk_topology_disk_names_finds_nested_leaves():
    topology = {'data': [_mirror('sda', 'sdb')], 'spare': [_leaf('sdc')]}
    names = set(pools._walk_topology_disk_names(topology))
    assert names == {'sda', 'sdb', 'sdc'}


def test_walk_topology_disk_names_tolerant_of_missing_keys():
    # No 'children', no 'disk' — must not raise.
    assert list(pools._walk_topology_disk_names({'data': [{'type': 'RAIDZ1'}]})) == []
    assert list(pools._walk_topology_disk_names(None)) == []
    assert list(pools._walk_topology_disk_names({'data': 'not-a-list'})) == []


def test_degraded_pool_disk_names_only_includes_unhealthy_pools():
    healthy_pool = _pool(name='healthy', status='ONLINE', healthy=True,
                          topology={'data': [_mirror('sda', 'sdb')]})
    degraded_pool = _pool(name='Backup_Proxmox', status='DEGRADED', healthy=False,
                           topology={'data': [_mirror('sdc', 'sdd')]})
    names = pools.degraded_pool_disk_names([healthy_pool, degraded_pool])
    assert names == {'sdc', 'sdd'}


def test_degraded_pool_disk_names_treats_healthy_false_as_degraded_even_if_status_online():
    pool = _pool(status='ONLINE', healthy=False, topology={'data': [_mirror('sda')]})
    assert pools.degraded_pool_disk_names([pool]) == {'sda'}


def test_temperatures_excludes_degraded_pool_disks():
    healthy_pool = _pool(name='healthy', topology={'data': [_mirror('sda')]})
    degraded_pool = _pool(name='bad', status='DEGRADED', healthy=False,
                           topology={'data': [_mirror('sdb')]})
    disks = [{'name': 'sda'}, {'name': 'sdb'}]
    conn = FakeConn({'disk.temperature_agg': {'sda': {'avg': 30}}})
    result = pools.temperatures(conn, disks=disks, pools=[healthy_pool, degraded_pool])
    assert result == {'sda': {'avg': 30}}
    method, params = conn.calls[0]
    assert method == 'disk.temperature_agg'
    assert params == [['sda']]  # 'sdb' excluded — belongs to the degraded pool


def test_temperatures_returns_empty_without_calling_rpc_when_all_disks_excluded():
    degraded_pool = _pool(status='DEGRADED', healthy=False, topology={'data': [_mirror('sda')]})
    conn = FakeConn({})  # would raise if disk.temperature_agg were called
    result = pools.temperatures(conn, disks=[{'name': 'sda'}], pools=[degraded_pool])
    assert result == {}
    assert conn.methods_called() == []


def test_health_reports_all_healthy():
    conn = FakeConn({})
    report = pools.pools.health(conn, pools=[_pool()])
    assert report.healthy is True
    assert report.details['unhealthy_pools'] == []


def test_health_reports_degraded_pool_by_name():
    bad = _pool(name='Backup_Proxmox', status='DEGRADED', healthy=False)
    report = pools.pools.health(conn=FakeConn({}), pools=[_pool(), bad])
    assert report.healthy is False
    assert 'Backup_Proxmox' in report.details['unhealthy_pools']


def test_read_finds_pool_by_name():
    conn = FakeConn({'pool.query': [_pool(name='tank')]})
    assert pools.pools.read(conn, 'tank')['name'] == 'tank'


def test_read_returns_none_for_unknown_pool():
    conn = FakeConn({'pool.query': [_pool(name='tank')]})
    assert pools.pools.read(conn, 'does-not-exist') is None
