# -*- coding: utf-8 -*-
"""Subsystem collectors (pools, datasets, snapshots, shares, replication,
apps_vms, system) implementing the ``Subsystem`` contract from
``core/subsystem.py`` (brief §2).

F1 (this phase): every module is READ-ONLY — ``list``/``read``/``health``
only. Writers (create/update/delete) land per-subsystem starting F2, behind
the dry-run/confirm/audit write-path (brief §5). See
PEGAPROX_PLUGIN_TRUENAS_BRIEF.md §1/§2/§4.2 for the phase table and the
TrueNAS method list each module wraps.
"""
