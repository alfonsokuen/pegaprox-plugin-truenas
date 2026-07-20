# -*- coding: utf-8 -*-
"""core.subsystem: the base Subsystem contract's defaults (every concrete
module overrides list/read/health, but the base class's own NotImplementedError
stubs and the read-only write() default need direct coverage too)."""

import pytest

from core.subsystem import HealthReport, ReadOnlySubsystem, Subsystem


def test_health_report_to_dict():
    report = HealthReport(healthy=True, summary='all good', details={'n': 1})
    assert report.to_dict() == {'healthy': True, 'summary': 'all good', 'details': {'n': 1}}


def test_health_report_details_defaults_to_empty_dict():
    report = HealthReport(healthy=False, summary='bad')
    assert report.details == {}


def test_base_subsystem_list_raises_not_implemented():
    sub = Subsystem()
    with pytest.raises(NotImplementedError):
        sub.list(conn=None)


def test_base_subsystem_read_raises_not_implemented():
    sub = Subsystem()
    with pytest.raises(NotImplementedError):
        sub.read(conn=None, id='x')


def test_base_subsystem_health_raises_not_implemented():
    sub = Subsystem()
    with pytest.raises(NotImplementedError):
        sub.health(conn=None)


def test_base_subsystem_write_raises_read_only_subsystem():
    sub = Subsystem()
    sub.SUBSYSTEM_ID = 'example'
    with pytest.raises(ReadOnlySubsystem) as exc_info:
        sub.write(conn=None, op='create', payload={})
    assert 'example' in str(exc_info.value)


def test_concrete_subsystems_inherit_read_only_write():
    """Every real F1 subsystem must still refuse writes via the shared
    default — none of them override write() (that's F2+)."""
    from subsystems.apps_vms import apps_vms
    from subsystems.datasets import datasets
    from subsystems.pools import pools
    from subsystems.replication import replication
    from subsystems.shares import shares
    from subsystems.snapshots import snapshots
    from subsystems.system import system

    for sub in (system, pools, datasets, snapshots, shares, replication, apps_vms):
        with pytest.raises(ReadOnlySubsystem):
            sub.write(conn=None, op='create', payload={})
