# -*- coding: utf-8 -*-
"""Pools & Disks subsystem: ``pool.query`` (status/healthy/topology/scan —
resilver/scrub progress), ``disk.query``, ``disk.temperature_agg``.

Read-only in F1 — ``pool.replace``/``pool.scrub.scrub`` etc. are F5.

Safety correction carried over from the brief's post-review adjustment
(§4.3/§9, 2026-07-20): the risk in polling pool state was originally
misattributed — ``pool.query``/its ``scan`` field reads kernel ZFS state
and does NOT touch the platters, so polling it on any schedule is harmless
even while a resilver is running. The REAL risk is SMART temperature
queries against a disk that is already failing or mid-resilver, which CAN
add real load. So temperature polling explicitly excludes every disk that
belongs to a currently-DEGRADED (or otherwise unhealthy) pool — it still
reports that disk's presence/status via ``pool.query``'s topology, just
never queries its temperature.
"""

from core.subsystem import HealthReport, Subsystem

_DEGRADED_STATUSES = {'DEGRADED', 'FAULTED', 'UNAVAIL'}


def list_pools(conn):
    """``pool.query`` — every pool, attrs passthrough (name/status/healthy/
    topology/scan/... exactly as the middleware returns them)."""
    return conn.call('pool.query') or []


def list_disks(conn):
    """``disk.query`` — every physical disk known to the appliance."""
    return conn.call('disk.query') or []


def _is_pool_unhealthy(pool):
    if not pool.get('healthy', True):
        return True
    return str(pool.get('status', '')).upper() in _DEGRADED_STATUSES


def _walk_vdev_disk_names(vdev):
    """Yield every leaf disk device name under one vdev node, recursing
    into ``children``. Defensive against any topology shape variance across
    TrueNAS versions — this is NOT pinned down by a live-verified fixture
    (only pool.query's top-level list was verified live this session), so
    it tolerates missing/renamed keys rather than assuming a fixed depth."""
    if not isinstance(vdev, dict):
        return
    disk = vdev.get('disk')
    if disk:
        yield disk
    for child in vdev.get('children') or []:
        yield from _walk_vdev_disk_names(child)


def _walk_topology_disk_names(topology):
    if not isinstance(topology, dict):
        return
    for vdev_group in topology.values():
        if not isinstance(vdev_group, list):
            continue
        for vdev in vdev_group:
            yield from _walk_vdev_disk_names(vdev)


def degraded_pool_disk_names(pools):
    """Device names (e.g. ``'sda'``) belonging to any pool that is
    DEGRADED/FAULTED/UNAVAIL or reports ``healthy: false`` — the exclusion
    set for temperature polling."""
    names = set()
    for pool in pools:
        if _is_pool_unhealthy(pool):
            names.update(_walk_topology_disk_names(pool.get('topology') or {}))
    return names


def temperatures(conn, disks=None, pools=None):
    """``disk.temperature_agg`` over every disk NOT belonging to a
    currently-degraded pool. Returns ``{}`` if there is nothing safe to
    query (e.g. every disk sits in a degraded pool) rather than calling the
    method with an empty name list.

    ``disks``/``pools`` let the caller reuse already-fetched results to
    avoid duplicate RPCs within the same request.
    """
    disks = disks if disks is not None else list_disks(conn)
    pools = pools if pools is not None else list_pools(conn)
    exclude = degraded_pool_disk_names(pools)
    names = [d.get('name') for d in disks if d.get('name') and d.get('name') not in exclude]
    if not names:
        return {}
    return conn.call('disk.temperature_agg', [names]) or {}


class PoolsSubsystem(Subsystem):
    SUBSYSTEM_ID = 'pools'

    def list(self, conn):
        return list_pools(conn)

    def read(self, conn, id):
        for pool in list_pools(conn):
            if pool.get('name') == id or str(pool.get('id')) == str(id):
                return pool
        return None

    def health(self, conn, pools=None):
        pools = pools if pools is not None else list_pools(conn)
        unhealthy = [p.get('name') for p in pools if _is_pool_unhealthy(p)]
        healthy = not unhealthy
        summary = ('all pools healthy' if healthy else
                   f"{len(unhealthy)} pool(s) degraded: {', '.join(unhealthy)}")
        return HealthReport(healthy=healthy, summary=summary, details={
            'pool_count': len(pools),
            'unhealthy_pools': unhealthy,
        })


pools = PoolsSubsystem()
