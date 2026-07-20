# -*- coding: utf-8 -*-
"""Apps/VMs subsystem: ``app.query`` (Docker-backed Apps on 25.10) and
``vm.query``. Read-only in F1 — start/stop/upgrade/redeploy are F3.

Version note (confirmed live against the real `.64` instance, 25.10.1,
2026-07-20): both ``app.query`` and ``vm.query`` exist and respond ``[]``
(no apps/VMs currently configured) on THIS exact version. The brief flags
(§4.2/§9) that 25.04 moved VMs to Incus's ``virt.instance.*`` namespace and
25.10 announced a move back to libvirt under the classic ``vm.*``
namespace — so ``vm.query`` responding at all here is consistent with that
reversion, not evidence the namespace is stable across every instance. NO
``virt.instance.*`` shim is implemented in F1: it would be speculative code
for a namespace that isn't in use on the only instance this plugin talks to
today. Add the shim (``core/compat.py``, per the brief's file layout) IF a
future instance (SACEI/INGESA/GeoSpace, or `.64` itself after an upgrade)
proves ``vm.query`` 404s/errors and ``virt.instance.query`` is what answers
instead — don't build it blind now.
"""

from core.subsystem import Subsystem


def list_apps(conn):
    return conn.call('app.query') or []


def list_vms(conn):
    return conn.call('vm.query') or []


class AppsVmsSubsystem(Subsystem):
    SUBSYSTEM_ID = 'apps_vms'

    def list(self, conn):
        """Returns a dict ({'apps': [...], 'vms': [...]}), not a flat list —
        same rationale as shares.py: two distinct TrueNAS collections, and
        the UI's own Apps/VMs tab (brief §6) treats them as separate card
        groups."""
        return {'apps': list_apps(conn), 'vms': list_vms(conn)}


apps_vms = AppsVmsSubsystem()
