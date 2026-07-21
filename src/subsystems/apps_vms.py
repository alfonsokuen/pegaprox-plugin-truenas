# -*- coding: utf-8 -*-
"""Apps/VMs subsystem: ``app.query`` (Docker-backed Apps on 25.10) and
``vm.query`` (read, F1) plus VM start/stop/restart and App start/stop/
redeploy (write, F5 — brief §5 dry-run/confirm/verify/audit pattern, same
shape as datasets/snapshots/services).

F5 schemas (method names, param shapes, sync-vs-job) were confirmed live
via ``core.get_methods`` under a real TrueNAS admin session — but neither
``vm.query`` nor ``app.query`` had any live row on `.64` at the time (both
return ``[]``, see the version note below), so unlike services' F4b
(toggled the harmless, already-disabled `ftp` service end-to-end) there was
no real VM/app to round-trip a live start/stop against. The write path
itself reuses the identical, already-tested build/execute/verify/audit
machinery as every other F2+ write; what's NOT independently live-verified
is that a real VM/app on `.64` actually reaches the expected post-write
state — re-check once one exists.

Read-only in F1 — start/stop/upgrade/redeploy are F5.

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
future instance (another client's TrueNAS, or `.64` itself after an upgrade)
proves ``vm.query`` 404s/errors and ``virt.instance.query`` is what answers
instead — don't build it blind now.
"""

from core.subsystem import Subsystem, safe_call
from core.ws_client import WRITE_TIMEOUT

_VM_OPS = ('start', 'stop', 'restart')
# Apps have no 'restart' — the closest TrueNAS equivalent is 'redeploy'
# (stop, pull latest images, restart), a MEANINGFULLY heavier operation
# (it can pull a new image version) than a plain restart. Confirmed live
# via core.get_methods: app.restart does not exist on TrueNAS-25.10.1.
# Naming it 'redeploy' rather than aliasing it to 'restart' keeps that
# distinction visible to the operator instead of quietly hiding it.
_APP_OPS = ('start', 'stop', 'redeploy')


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
        groups. Each fetched independently via safe_call — the vm.query
        namespace is the one flagged as unstable across TrueNAS versions
        (see module docstring), so a failure there must not also hide
        `apps`, which responded fine (silent-failure-hunter finding, F1
        review round 2)."""
        apps, apps_error = safe_call('app.query', lambda: list_apps(conn), [])
        vms, vms_error = safe_call('vm.query', lambda: list_vms(conn), [])
        return {'apps': apps, 'apps_error': apps_error, 'vms': vms, 'vms_error': vms_error}


apps_vms = AppsVmsSubsystem()


def find_vm(conn, vm_id):
    for vm in list_vms(conn):
        if str(vm.get('id')) == str(vm_id):
            return vm
    return None


def find_app(conn, app_name):
    for app in list_apps(conn):
        if app.get('name') == app_name:
            return app
    return None


def build_vm_control_envelope(op, vm_id):
    """Pure builder — see brief §5 / datasets.py's module docstring for why
    dry-run and execute must share this exact function. VM ids are
    integers (confirmed live via ``vm.start``'s schema: ``id: integer``),
    unlike services (name string) and apps (``app_name`` string)."""
    if op not in _VM_OPS:
        raise ValueError(f"unknown vm op '{op}' (expected one of {_VM_OPS})")
    try:
        vm_id = int(vm_id)
    except (TypeError, ValueError):
        raise ValueError('vm_id must be an integer')
    if op == 'start':
        return 'vm.start', [vm_id, {'overcommit': False}]
    if op == 'stop':
        return 'vm.stop', [vm_id, {'force': False, 'force_after_timeout': False}]
    return 'vm.restart', [vm_id]


def control_vm(conn, op, vm_id):
    """``vm.stop``/``vm.restart`` are ``@job``-decorated (confirmed live —
    return an int job id); ``vm.start`` is synchronous. The write path's
    existing job_id handling (routes/api.py's writes_execute_handler)
    already covers both without change."""
    method, params = build_vm_control_envelope(op, vm_id)
    return conn.call(method, params, timeout=WRITE_TIMEOUT)


def build_app_control_envelope(op, app_name):
    if op not in _APP_OPS:
        raise ValueError(f"unknown app op '{op}' (expected one of {_APP_OPS})")
    app_name = str(app_name or '').strip()
    if not app_name:
        raise ValueError('app_name is required')
    return f'app.{op}', [app_name]


def control_app(conn, op, app_name):
    """``app.start``/``stop``/``redeploy`` are all ``@job``-decorated
    (confirmed live) — same job_id handling as VMs above."""
    method, params = build_app_control_envelope(op, app_name)
    return conn.call(method, params, timeout=WRITE_TIMEOUT)
