# -*- coding: utf-8 -*-
"""Flask route handlers for the TrueNAS plugin, dispatched by PegaProx's
plugin catch-all under ``/api/plugins/truenas/api/<path>``.

RBAC (brief §2 — PegaProx's PERMISSIONS table is fixed, a plugin cannot
register new verbs):
  GET  ui                                  -> UI shell                (storage.view)
  GET  config                              -> instances (masked)+poll (admin)
  POST config/save                         -> validate + persist      (admin)
  POST instances/test                      -> connect+login only      (admin)
  GET  system|pools|datasets|snapshots|
       shares|replication|apps_vms|
       services|data_protection|telemetry  -> subsystem read (F1/F4a/F6/telemetry) (storage.view)
  GET  fleet                               -> cross-instance summary (F3) (storage.view)

Every F1 subsystem route takes ``instance_id`` as a QUERY PARAM (e.g.
``GET .../pools?instance_id=datos-64``), not a URL path segment. This is a
deliberate deviation from the brief's illustrative
``/<instance_id>/<subsystem>`` phrasing: the only CONFIRMED-in-production
plugin routing mechanism (``pegaprox.api.plugins.register_plugin_route``,
verified against ``pegaprox-plugin-wake-on-lan``) maps one FIXED path
string per handler — it does not support Flask-style path parameters.
wake-on-lan's own dynamic routes (``job``, `status`) already use query
params (``request.args.get('job_id')``) for exactly this reason, so this
follows the one pattern actually proven to work rather than assuming
PegaProx's catch-all supports URL templating it hasn't been observed to
support.

Config/instances-test require the admin role outright (they touch API
keys, mirroring wake-on-lan's SSH-credentials gate); the F1 read routes
only require ``storage.view`` — they never see a key, only a subsystem's
read-only JSON-RPC results.
"""

import hashlib
import json
import logging
import os
import threading
import time

from flask import request, jsonify, send_file

from pegaprox.utils.auth import load_users
from pegaprox.utils.rbac import has_permission
from pegaprox.utils.audit import log_audit

from core.conn_manager import ConnectionManager
from core.errors import TrueNASAuthError, TrueNASError
from core.subsystem import ConfirmationRequired, safe_call
import subsystems.apps_vms as apps_vms_mod
import subsystems.datasets as datasets_mod
import subsystems.fleet as fleet_mod
import subsystems.services as services_mod
import subsystems.shares as shares_mod
import subsystems.snapshots as snapshots_mod
import subsystems.telemetry as telemetry_mod
from subsystems.apps_vms import apps_vms as apps_vms_subsystem
from subsystems.data_protection import data_protection as data_protection_subsystem
from subsystems.datasets import datasets as datasets_subsystem
from subsystems.pools import list_disks as pools_list_disks
from subsystems.pools import pools as pools_subsystem
from subsystems.pools import temperatures as pools_temperatures
from subsystems.replication import replication as replication_subsystem
from subsystems.services import services as services_subsystem
from subsystems.shares import shares as shares_subsystem
from subsystems.snapshots import list_tasks as snapshots_list_tasks
from subsystems.snapshots import snapshots as snapshots_subsystem
from subsystems.system import alerts as system_alerts
from subsystems.system import info as system_info
from subsystems.system import system as system_subsystem
from subsystems.system import update_status as system_update_status
from . import config_store

PLUGIN_ID = 'truenas'
PERM_VIEW = 'storage.view'
MASK = config_store.MASK

log = logging.getLogger(f'plugin.{PLUGIN_ID}')

CONFIG_PATH = None   # set by init()
UI_HTML_PATH = None  # set by init()

conn_manager = ConnectionManager()


def init(plugin_dir):
    """Wire the module-level paths to the real plugin directory. Called once
    from ``__init__.py``'s ``register()`` (and directly by tests with a
    scratch dir) — kept separate from import time so nothing touches the
    filesystem just by importing this module."""
    global CONFIG_PATH, UI_HTML_PATH
    CONFIG_PATH = os.path.join(plugin_dir, 'config.json')
    UI_HTML_PATH = os.path.join(plugin_dir, 'src', 'ui', 'plugin.html')


# ---------------------------------------------------------------------------
# Auth helpers (same shape as wake-on-lan's)
# ---------------------------------------------------------------------------

def _current_user():
    users = load_users()
    return users.get(request.session.get('user'), {})


def _username():
    return request.session.get('user', 'system')


def _require(perm):
    if not has_permission(_current_user(), perm):
        return jsonify({'error': 'Permission denied', 'required': perm}), 403
    return None


def _require_admin():
    from pegaprox.models.permissions import ROLE_ADMIN
    if _current_user().get('role') != ROLE_ADMIN:
        return jsonify({'error': 'Admin access required'}), 403
    return None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def ui_handler():
    if (err := _require(PERM_VIEW)):
        return err
    if UI_HTML_PATH and os.path.exists(UI_HTML_PATH):
        return send_file(UI_HTML_PATH, mimetype='text/html')
    return jsonify({'error': 'UI not found'}), 404


def config_handler():
    if (err := _require_admin()):
        return err
    cfg = config_store.load_config(CONFIG_PATH)
    masked = [config_store.mask_instance(i) for i in cfg['instances']]
    return jsonify({
        'instances': masked,
        'instances_by_client': config_store.group_by_client(masked),
        'poll': cfg['poll'],
    })


def config_save_handler():
    if (err := _require_admin()):
        return err
    raw_body = request.get_json(silent=True)
    if raw_body is None:
        # get_json(silent=True) returns None for BOTH "no body" and
        # "malformed JSON" — collapsing that to `{}` earlier made every
        # validator fail with a misleading "instances must be a list" /
        # "host is required" instead of telling the operator their request
        # body simply wasn't valid JSON.
        return jsonify({'error': 'request body must be valid JSON'}), 400
    body = raw_body if isinstance(raw_body, dict) else {}
    old_cfg = config_store.load_config(CONFIG_PATH)

    instances, err = config_store.validate_instances(
        body.get('instances'), old_cfg['instances'])
    if err:
        return jsonify({'error': err}), 400

    poll, err = config_store.validate_poll(body.get('poll'))
    if err:
        return jsonify({'error': err}), 400

    cfg = {'instances': instances, 'poll': poll}
    try:
        config_store.save_config(CONFIG_PATH, cfg)
    except OSError as e:
        # e.g. disk full, permission denied — must not escape as an
        # unhandled 500 with no context for whoever reads the PegaProx logs.
        log.error(f'[{PLUGIN_ID}] failed to persist config.json: {e}', exc_info=True)
        return jsonify({'error': f'could not save config: {e}'}), 500
    # Credentials/host may have changed — drop any live sockets so the next
    # call reconnects against the freshly saved config, never a stale key.
    conn_manager.close_all()
    log_audit(user=_username(), action='truenas.config_saved',
              details=f'{len(instances)} instance(s)')
    return jsonify({'ok': True, 'instances': len(instances)})


def instances_test_handler():
    """The ONLY real interaction with a TrueNAS instance allowed in F0:
    connect + auth.login_with_api_key, nothing else. Never writes, never
    persists. Accepts either a saved ``id`` (uses the stored api_key_ro) or
    a full draft payload from an unsaved Settings form (so the operator can
    test before hitting Save)."""
    if (err := _require_admin()):
        return err
    raw_body = request.get_json(silent=True)
    if raw_body is None:
        return jsonify({'ok': False, 'error': 'request body must be valid JSON'}), 400
    body = raw_body if isinstance(raw_body, dict) else {}
    cfg = config_store.load_config(CONFIG_PATH)

    instance_id = str(body.get('id') or '').strip()
    saved = config_store.find_instance(cfg['instances'], instance_id) if instance_id else None

    host = str(body.get('host') or (saved or {}).get('host') or '').strip()
    raw_port = body.get('port', (saved or {}).get('port'))
    use_tls = body.get('use_tls', (saved or {}).get('use_tls', True))
    verify_tls = body.get('verify_tls', (saved or {}).get('verify_tls', False))
    api_key_ro = body.get('api_key_ro')
    if api_key_ro in (None, '', MASK):
        api_key_ro = (saved or {}).get('api_key_ro')

    if not host or not api_key_ro:
        return jsonify({'ok': False, 'error': 'host and api_key_ro are required'}), 400
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'port must be an integer'}), 400

    # Same hard guard as config_store.validate_instances (save path): a real
    # API key must never travel over plain ws:// — TrueNAS auto-revokes it
    # on first use over HTTP. Without this check here, an operator could
    # untick "use_tls" on the draft form and hit "Probar conexión" BEFORE
    # saving, revoking their own production key with a single click.
    if not use_tls:
        return jsonify({'ok': False, 'error': (
            'use_tls debe ser true si hay una API key configurada (TrueNAS '
            'revoca la key automáticamente sobre HTTP plano)')}), 400

    tls_server_name = body.get('tls_server_name', (saved or {}).get('tls_server_name'))
    instance_cfg = {
        'id': instance_id or f'test-{host}',
        'host': host, 'port': port,
        'use_tls': bool(use_tls), 'verify_tls': bool(verify_tls),
        'tls_server_name': tls_server_name or None,
    }
    result = conn_manager.test_connection(instance_cfg, api_key_ro)
    client_id = (saved or {}).get('client_id', 'unassigned')
    log_audit(user=_username(), action='truenas.instance_test',
              details=(f"instance={instance_cfg['id']} client={client_id} "
                       f"host={host} ok={result['ok']}"))
    return jsonify(result)


# ---------------------------------------------------------------------------
# F1: subsystem read routes — shared instance resolution + error handling
# ---------------------------------------------------------------------------

def _resolve_instance(instance_id):
    """Look up ``instance_id`` in config.json and validate it's usable for
    a read. Returns ``(instance_dict, None)`` or ``(None, error_response)``
    where ``error_response`` is a ready-to-return ``(jsonify(...), status)``
    tuple — never a bare 500, same standard as ``config``/``instances/test``."""
    if not instance_id:
        return None, (jsonify({'error': 'instance_id is required'}), 400)
    cfg = config_store.load_config(CONFIG_PATH)
    inst = config_store.find_instance(cfg['instances'], instance_id)
    if not inst:
        return None, (jsonify({'error': f"instance '{instance_id}' not found"}), 404)
    if not inst.get('api_key_ro'):
        return None, (jsonify({
            'error': f"instance '{instance_id}' has no api_key_ro configured — "
                     f"add one from Settings before viewing this tab"}), 400)
    return inst, None


def _get_authenticated_connection(inst):
    """Return the (cached, persistent) client for ``inst``, logged in with
    ``api_key_ro`` if the CURRENT socket hasn't authenticated yet.
    Read-only routes always use the RO key — never RW, even if configured
    (brief §3: minimum privilege in runtime, regardless of the instance's
    ``readonly`` flag, which gates writers, not this).

    Fails fast on ``conn.needs_auth`` — set when the appliance already
    rejected this same key on a previous relogin attempt (bad/revoked key).
    Without this check, every request/poll would retry the identical login
    call against a key already known to be bad, hammering the appliance
    with failed-auth attempts for no benefit (the error would still reach
    the user either way — this just stops repeating a call whose answer is
    already known)."""
    conn = conn_manager.get_connection(inst)
    if conn.needs_auth:
        raise TrueNASAuthError('auth.login_with_api_key', {
            'message': 'API key was rejected on a previous attempt — '
                       'check/rotate it in Settings before retrying'})
    if not conn.is_authenticated:
        conn.login(inst['api_key_ro'])
    return conn


def _subsystem_route(fetch_fn):
    """Shared body for every F1 read-only subsystem route: permission gate,
    instance resolution via the ``instance_id`` query param, lazy
    connect+login, and TrueNAS-error -> clear-context JSON (never a bare,
    unexplained 500). ``fetch_fn(conn) -> JSON-serializable data``.
    """
    if (err := _require(PERM_VIEW)):
        return err
    instance_id = request.args.get('instance_id', '').strip()
    inst, err_resp = _resolve_instance(instance_id)
    if err_resp:
        return err_resp
    try:
        conn = _get_authenticated_connection(inst)
        data = fetch_fn(conn)
    except TrueNASError as e:
        # This is the EXPECTED failure path (appliance down, timeout,
        # revoked key) — it used to leave zero trace server-side, so at
        # 3am there was no way to tell from the logs that an instance had
        # been 502ing for hours; only whoever happened to have the tab
        # open in a browser ever saw it. Always log it, even though it's
        # not a bug.
        log.warning(f"[{PLUGIN_ID}] TrueNAS error for instance "
                    f"'{instance_id}': {e}")
        return jsonify({'error': str(e), 'instance_id': instance_id}), 502
    except Exception as e:  # defensive: never let a subsystem bug 500 mute
        log.error(f"[{PLUGIN_ID}] unexpected error in subsystem route for "
                  f"instance '{instance_id}': {e}", exc_info=True)
        return jsonify({'error': f'unexpected error: {e}', 'instance_id': instance_id}), 500
    return jsonify({'instance_id': instance_id, 'data': data})


def _system_fetch(conn):
    """Every sub-call degrades independently (safe_call) — a failing
    update.status (the LEAST critical call here, and per this module's own
    docstring the one whose "no update available" shape was never captured
    live) must not also hide alerts/health, which is what an all-or-nothing
    fetch used to do (silent-failure-hunter finding, F1 review round 2)."""
    info, info_error = safe_call('system.info', lambda: system_subsystem.read(conn), {})
    active_alerts, alerts_error = safe_call('alert.list', lambda: system_alerts(conn), [])
    update_status, update_status_error = safe_call(
        'update.status', lambda: system_update_status(conn), {})
    return {
        'info': info, 'info_error': info_error,
        'alerts': active_alerts, 'alerts_error': alerts_error,
        'update_status': update_status, 'update_status_error': update_status_error,
        'health': system_subsystem.health(conn, active_alerts=active_alerts).to_dict(),
    }


def system_handler():
    return _subsystem_route(_system_fetch)


def _pools_fetch(conn):
    """``pool.query`` itself is NOT wrapped in safe_call — with no pools at
    all there is nothing meaningful left to show, so that failure legitimately
    surfaces as the route's 502. But ``disk.query`` and, especially,
    ``disk.temperature_agg`` degrade independently: the real risk scenario
    (brief §4.3/§9) is a disk failing SMART in a pool that's still ONLINE —
    exactly where a hung/erroring temperature query must not also take down
    pool status/health, which is what an all-or-nothing fetch used to do."""
    pool_list = pools_subsystem.list(conn)
    disks, disks_error = safe_call('disk.query', lambda: pools_list_disks(conn), [])
    temperatures, temperatures_error = safe_call(
        'disk.temperature_agg',
        lambda: pools_temperatures(conn, disks=disks, pools=pool_list), {})
    return {
        'pools': pool_list,
        'disks': disks, 'disks_error': disks_error,
        'temperatures': temperatures, 'temperatures_error': temperatures_error,
        'health': pools_subsystem.health(conn, pools=pool_list).to_dict(),
    }


def pools_handler():
    return _subsystem_route(_pools_fetch)


def datasets_handler():
    return _subsystem_route(datasets_subsystem.list)


def _snapshots_fetch(conn):
    return {
        'snapshots': snapshots_subsystem.list(conn),
        'tasks': snapshots_list_tasks(conn),
    }


def snapshots_handler():
    return _subsystem_route(_snapshots_fetch)


def shares_handler():
    return _subsystem_route(shares_subsystem.list)


def replication_handler():
    return _subsystem_route(replication_subsystem.list)


def apps_vms_handler():
    return _subsystem_route(apps_vms_subsystem.list)


def services_handler():
    return _subsystem_route(services_subsystem.list)


def data_protection_handler():
    return _subsystem_route(data_protection_subsystem.list)


def _telemetry_fetch(conn):
    """Needs ``system.info``'s ``physmem`` to turn memory's raw 'available
    bytes' series into a used%; a second, independent ``system.info`` call
    from the one the Overview tab's own 'system' route makes — cheap, and
    keeps this route self-contained rather than coupling to another
    route's fetch order."""
    info, info_error = safe_call('system.info', lambda: system_info(conn), {})
    data = telemetry_mod.telemetry(conn, physmem=info.get('physmem'))
    if info_error:
        data['physmem_error'] = info_error
    return data


def telemetry_handler():
    return _subsystem_route(_telemetry_fetch)


# ---------------------------------------------------------------------------
# F3: Fleet Overview — fans out over EVERY configured instance concurrently
# (no ``instance_id`` query param), so it does not go through
# ``_subsystem_route``. TTL-cached with a bare in-process dict guarded by a
# lock (not per-user — the underlying data is the same regardless of who is
# looking) so a UI poll tick never re-hammers every appliance on every
# request; ``fleet.py``'s own per-RPC ``safe_call`` + per-instance isolation
# already bounds the cost of a single fetch, this just bounds how often that
# fetch happens at all.
# ---------------------------------------------------------------------------

_FLEET_CACHE_TTL_S = 15.0
_fleet_cache = {'at': 0.0, 'data': None}
_fleet_cache_lock = threading.Lock()


def _fleet_get_conn(inst):
    return _get_authenticated_connection(inst)


def fleet_handler():
    if (err := _require(PERM_VIEW)):
        return err
    now = time.monotonic()
    with _fleet_cache_lock:
        if _fleet_cache['data'] is not None and (now - _fleet_cache['at']) < _FLEET_CACHE_TTL_S:
            return jsonify(_fleet_cache['data'])

    cfg = config_store.load_config(CONFIG_PATH)
    instances = [i for i in cfg['instances'] if i.get('api_key_ro')]
    skipped_no_key = len(cfg['instances']) - len(instances)
    summaries = fleet_mod.fetch_fleet(instances, _fleet_get_conn)
    payload = {
        'instances': summaries,
        'aggregate': fleet_mod.aggregate(summaries),
        'skipped_no_api_key': skipped_no_key,
    }
    with _fleet_cache_lock:
        _fleet_cache['at'] = now
        _fleet_cache['data'] = payload
    return jsonify(payload)


# ---------------------------------------------------------------------------
# F2: write path (brief §5) — datasets/snapshots create/update/delete.
#
# Every op is registered ONCE in WRITE_OPS as a (build, execute, verify)
# triple. ``writes/dry-run`` calls ONLY ``build`` (no ``conn`` parameter at
# all — not a convention, a structural guarantee it cannot touch TrueNAS).
# ``writes/execute`` calls the exact same ``build`` first (so validation —
# including the typed-confirmation guard on deletes — happens identically
# in both paths) and only then ``execute`` against a real, RW-authenticated
# connection. This is what makes it structurally impossible for a dry-run
# preview to describe a different JSON-RPC call than what execute actually
# runs — the alternative (building the envelope twice, once per path) is
# exactly the kind of trap that silently desyncs over time.
#
# Sync-vs-async (documented, unresolved without live access — see
# datasets.py's module docstring): every ``execute`` result is treated as
# POSSIBLY an int job id. The post-write verify (step 6) re-reads the
# resource regardless; if verify doesn't yet show the expected state AND
# the result was an int, this is reported as ``status: "pending"``
# (genuinely unknown: job still running vs. actually failed) rather than
# asserted as success or failure — no job poller is built in F2 (out of
# scope per this phase), so "pending" comes with a re-check path (call the
# same route again) instead of a false verdict either way (step 8: no
# auto-retry, real status + a way to re-check).
# ---------------------------------------------------------------------------

def _dataset_create_build(payload):
    return datasets_mod.build_create_envelope(payload)


def _dataset_create_execute(conn, payload):
    return datasets_mod.create(conn, payload)


def _verify_dataset_created(conn, payload, result):
    found = datasets_subsystem.read(conn, payload.get('name'))
    return found is not None, found


def _dataset_update_build(payload):
    return datasets_mod.build_update_envelope(payload.get('dataset_id'), payload.get('changes') or {})


def _dataset_update_execute(conn, payload):
    return datasets_mod.update(conn, payload.get('dataset_id'), payload.get('changes') or {})


# pool.dataset.update accepts write-only CONTROL params that are never
# persisted dataset properties — a re-read will never echo them back, so
# comparing them would always "mismatch" even on a fully successful
# update. force_size ("bypass the >80%-available guard on a zvol resize",
# brief §4.2) is the one named explicitly in the brief; excluded from
# verification, not from the actual write payload sent to TrueNAS.
_UPDATE_CONTROL_ONLY_FIELDS = {'force_size'}


def _dataset_field_matches(actual, expected):
    """Best-effort compare one re-read TrueNAS dataset field against the
    value a write requested. TrueNAS commonly nests dataset properties as
    ``{'parsed': ..., 'rawvalue': ...}``; unwrap those before comparing.
    Not live-confirmed for this exact shape this session — deliberately
    returns False (i.e. "not confirmed as matching") rather than raising
    on any structure it doesn't recognize, so an unexpected shape shows up
    as an unconfirmed change, never a crash."""
    if isinstance(actual, dict):
        if 'parsed' in actual:
            return actual['parsed'] == expected
        if 'rawvalue' in actual:
            return str(actual['rawvalue']) == str(expected)
        return False
    return actual == expected


def _verify_dataset_updated(conn, payload, result):
    """Re-reads the dataset and compares every field in
    ``payload['changes']`` against it. A bare "does the dataset still
    exist" check (the previous implementation) was vacuously true even
    BEFORE the update ran — it could never catch an update that silently
    didn't apply, or distinguish a still-running async job from a real
    success (code-reviewer + silent-failure-hunter finding, F2 review
    round 2: the 'pending' branch was unreachable for updates because
    this always reported True).

    Design choice (documented per the coordinator's explicit request):
    field comparison was chosen over unconditionally forcing 'pending' on
    an int result, because it gives a REAL signal (verified/not) when the
    write turns out to be synchronous — which the brief's own uncertainty
    note treats as at least as likely as async. The existing job_id logic
    in the caller already falls through to 'pending' whenever this
    returns False AND the result was an int, so the two approaches
    compose: a genuine mismatch on a synchronous write still surfaces as
    'verify_failed' (a real problem), while the same mismatch after an
    async int result surfaces as 'pending' (genuinely unknown) — never a
    false 'ok' either way.
    """
    dataset_id = payload.get('dataset_id')
    found = datasets_subsystem.read(conn, dataset_id)
    if found is None:
        return False, None
    changes = payload.get('changes') or {}
    comparable = {k: v for k, v in changes.items() if k not in _UPDATE_CONTROL_ONLY_FIELDS}
    if not comparable:
        return True, found
    all_confirmed = all(
        _dataset_field_matches(found.get(key), expected)
        for key, expected in comparable.items()
    )
    return all_confirmed, found


def _dataset_delete_build(payload):
    return datasets_mod.build_delete_envelope(
        payload.get('dataset_id'), payload.get('confirm_name'), payload.get('options'))


def _dataset_delete_execute(conn, payload):
    return datasets_mod.delete(
        conn, payload.get('dataset_id'), payload.get('confirm_name'), payload.get('options'))


def _verify_dataset_deleted(conn, payload, result):
    found = datasets_subsystem.read(conn, payload.get('dataset_id'))
    return found is None, found


def _snapshot_create_build(payload):
    return snapshots_mod.build_create_envelope(
        payload.get('dataset'), payload.get('name'), payload.get('recursive', False))


def _snapshot_create_execute(conn, payload):
    return snapshots_mod.create(
        conn, payload.get('dataset'), payload.get('name'), payload.get('recursive', False))


def _verify_snapshot_created(conn, payload, result):
    expected_id = f"{payload.get('dataset')}@{payload.get('name')}"
    found = snapshots_subsystem.read(conn, expected_id)
    return found is not None, found


def _snapshot_delete_build(payload):
    return snapshots_mod.build_delete_envelope(payload.get('snapshot_id'), payload.get('confirm_name'))


def _snapshot_delete_execute(conn, payload):
    return snapshots_mod.delete(conn, payload.get('snapshot_id'), payload.get('confirm_name'))


def _verify_snapshot_deleted(conn, payload, result):
    found = snapshots_subsystem.read(conn, payload.get('snapshot_id'))
    return found is None, found


# F4b — service start/stop/restart. Each op's build/execute pair calls the
# SAME services_mod.build_control_envelope()/control() so dry-run and
# execute can never diverge, same guarantee as the dataset/snapshot ops
# above. verify() re-reads the service and checks the state a successful
# op should leave it in — restart's target is RUNNING (same as start's),
# not "different from before", since a restart that leaves the service
# down is exactly the failure an operator needs surfaced as verify_failed.

def _service_start_build(payload):
    return services_mod.build_control_envelope('start', payload.get('service'))


def _service_start_execute(conn, payload):
    return services_mod.control(conn, 'start', payload.get('service'))


def _service_stop_build(payload):
    return services_mod.build_control_envelope('stop', payload.get('service'))


def _service_stop_execute(conn, payload):
    return services_mod.control(conn, 'stop', payload.get('service'))


def _service_restart_build(payload):
    return services_mod.build_control_envelope('restart', payload.get('service'))


def _service_restart_execute(conn, payload):
    return services_mod.control(conn, 'restart', payload.get('service'))


def _verify_service_state(conn, payload, expected_state):
    found = services_subsystem.read(conn, payload.get('service'))
    if found is None:
        return False, None
    return str(found.get('state', '')).upper() == expected_state, found


def _verify_service_running(conn, payload, result):
    return _verify_service_state(conn, payload, 'RUNNING')


def _verify_service_stopped(conn, payload, result):
    return _verify_service_state(conn, payload, 'STOPPED')


# F5 — VM start/stop/restart and App start/stop/redeploy. Same build/
# execute/verify split; VMs are keyed by integer id, apps by string name
# (matches vm.start's `id: integer` vs app.start's `app_name: string`
# schemas, confirmed live).

def _vm_start_build(payload):
    return apps_vms_mod.build_vm_control_envelope('start', payload.get('vm_id'))


def _vm_start_execute(conn, payload):
    return apps_vms_mod.control_vm(conn, 'start', payload.get('vm_id'))


def _vm_stop_build(payload):
    return apps_vms_mod.build_vm_control_envelope('stop', payload.get('vm_id'))


def _vm_stop_execute(conn, payload):
    return apps_vms_mod.control_vm(conn, 'stop', payload.get('vm_id'))


def _vm_restart_build(payload):
    return apps_vms_mod.build_vm_control_envelope('restart', payload.get('vm_id'))


def _vm_restart_execute(conn, payload):
    return apps_vms_mod.control_vm(conn, 'restart', payload.get('vm_id'))


def _verify_vm_state(conn, payload, expected_states):
    found = apps_vms_mod.find_vm(conn, payload.get('vm_id'))
    if found is None:
        return False, None
    state = str((found.get('status') or {}).get('state', '')).upper()
    return state in expected_states, found


def _verify_vm_running(conn, payload, result):
    return _verify_vm_state(conn, payload, {'RUNNING'})


def _verify_vm_stopped(conn, payload, result):
    return _verify_vm_state(conn, payload, {'STOPPED'})


def _app_start_build(payload):
    return apps_vms_mod.build_app_control_envelope('start', payload.get('app_name'))


def _app_start_execute(conn, payload):
    return apps_vms_mod.control_app(conn, 'start', payload.get('app_name'))


def _app_stop_build(payload):
    return apps_vms_mod.build_app_control_envelope('stop', payload.get('app_name'))


def _app_stop_execute(conn, payload):
    return apps_vms_mod.control_app(conn, 'stop', payload.get('app_name'))


def _app_redeploy_build(payload):
    return apps_vms_mod.build_app_control_envelope('redeploy', payload.get('app_name'))


def _app_redeploy_execute(conn, payload):
    return apps_vms_mod.control_app(conn, 'redeploy', payload.get('app_name'))


def _verify_app_state(conn, payload, expected_states):
    found = apps_vms_mod.find_app(conn, payload.get('app_name'))
    if found is None:
        return False, None
    return str(found.get('state', '')).upper() in expected_states, found


def _verify_app_running(conn, payload, result):
    return _verify_app_state(conn, payload, {'RUNNING'})


def _verify_app_stopped(conn, payload, result):
    return _verify_app_state(conn, payload, {'STOPPED'})


# F4c — SMB/NFS share create/update/delete. Verified live against real,
# actively-used shares on `.64` ("nextcloud" SMB, "PBS_NFS" NFS backing
# Proxmox Backup Server) that both are synchronous (no job_id handling
# needed). Delete's confirmation compares against ``expected_name``/
# ``expected_path`` the CALLER already knows (the UI row at click-time),
# not looked up here — see shares.py's module docstring for why an opaque
# share id can't use the same "id IS the human string" shortcut datasets
# use.

def _smb_create_build(payload):
    return shares_mod.build_smb_create_envelope(payload.get('fields') or {})


def _smb_create_execute(conn, payload):
    return shares_mod.smb_create(conn, payload.get('fields') or {})


def _verify_smb_created(conn, payload, result):
    name = (payload.get('fields') or {}).get('name')
    found = next((s for s in shares_mod.list_smb(conn) if s.get('name') == name), None)
    return found is not None, found


def _smb_update_build(payload):
    return shares_mod.build_smb_update_envelope(payload.get('share_id'), payload.get('fields') or {})


def _smb_update_execute(conn, payload):
    return shares_mod.smb_update(conn, payload.get('share_id'), payload.get('fields') or {})


def _verify_smb_updated(conn, payload, result):
    found = shares_mod.find_smb(conn, payload.get('share_id'))
    if found is None:
        return False, None
    changes = payload.get('fields') or {}
    matches = all(found.get(k) == v for k, v in changes.items())
    return matches, found


def _smb_delete_build(payload):
    return shares_mod.build_smb_delete_envelope(
        payload.get('share_id'), payload.get('confirm_name'), payload.get('expected_name'))


def _smb_delete_execute(conn, payload):
    return shares_mod.smb_delete(
        conn, payload.get('share_id'), payload.get('confirm_name'), payload.get('expected_name'))


def _verify_smb_deleted(conn, payload, result):
    found = shares_mod.find_smb(conn, payload.get('share_id'))
    return found is None, found


def _nfs_create_build(payload):
    return shares_mod.build_nfs_create_envelope(payload.get('fields') or {})


def _nfs_create_execute(conn, payload):
    return shares_mod.nfs_create(conn, payload.get('fields') or {})


def _verify_nfs_created(conn, payload, result):
    path = (payload.get('fields') or {}).get('path')
    found = next((s for s in shares_mod.list_nfs(conn) if s.get('path') == path), None)
    return found is not None, found


def _nfs_update_build(payload):
    return shares_mod.build_nfs_update_envelope(payload.get('share_id'), payload.get('fields') or {})


def _nfs_update_execute(conn, payload):
    return shares_mod.nfs_update(conn, payload.get('share_id'), payload.get('fields') or {})


def _verify_nfs_updated(conn, payload, result):
    found = shares_mod.find_nfs(conn, payload.get('share_id'))
    if found is None:
        return False, None
    changes = payload.get('fields') or {}
    matches = all(found.get(k) == v for k, v in changes.items())
    return matches, found


def _nfs_delete_build(payload):
    return shares_mod.build_nfs_delete_envelope(
        payload.get('share_id'), payload.get('confirm_name'), payload.get('expected_path'))


def _nfs_delete_execute(conn, payload):
    return shares_mod.nfs_delete(
        conn, payload.get('share_id'), payload.get('confirm_name'), payload.get('expected_path'))


def _verify_nfs_deleted(conn, payload, result):
    found = shares_mod.find_nfs(conn, payload.get('share_id'))
    return found is None, found


WRITE_OPS = {
    ('datasets', 'create'): {
        'build': _dataset_create_build, 'execute': _dataset_create_execute,
        'verify': _verify_dataset_created,
    },
    ('datasets', 'update'): {
        'build': _dataset_update_build, 'execute': _dataset_update_execute,
        'verify': _verify_dataset_updated,
    },
    ('datasets', 'delete'): {
        'build': _dataset_delete_build, 'execute': _dataset_delete_execute,
        'verify': _verify_dataset_deleted,
    },
    ('snapshots', 'create'): {
        'build': _snapshot_create_build, 'execute': _snapshot_create_execute,
        'verify': _verify_snapshot_created,
    },
    ('snapshots', 'delete'): {
        'build': _snapshot_delete_build, 'execute': _snapshot_delete_execute,
        'verify': _verify_snapshot_deleted,
    },
    ('services', 'start'): {
        'build': _service_start_build, 'execute': _service_start_execute,
        'verify': _verify_service_running,
    },
    ('services', 'stop'): {
        'build': _service_stop_build, 'execute': _service_stop_execute,
        'verify': _verify_service_stopped,
    },
    ('services', 'restart'): {
        'build': _service_restart_build, 'execute': _service_restart_execute,
        'verify': _verify_service_running,
    },
    ('vms', 'start'): {
        'build': _vm_start_build, 'execute': _vm_start_execute,
        'verify': _verify_vm_running,
    },
    ('vms', 'stop'): {
        'build': _vm_stop_build, 'execute': _vm_stop_execute,
        'verify': _verify_vm_stopped,
    },
    ('vms', 'restart'): {
        'build': _vm_restart_build, 'execute': _vm_restart_execute,
        'verify': _verify_vm_running,
    },
    ('apps', 'start'): {
        'build': _app_start_build, 'execute': _app_start_execute,
        'verify': _verify_app_running,
    },
    ('apps', 'stop'): {
        'build': _app_stop_build, 'execute': _app_stop_execute,
        'verify': _verify_app_stopped,
    },
    ('apps', 'redeploy'): {
        'build': _app_redeploy_build, 'execute': _app_redeploy_execute,
        'verify': _verify_app_running,
    },
    ('smb_shares', 'create'): {
        'build': _smb_create_build, 'execute': _smb_create_execute,
        'verify': _verify_smb_created,
    },
    ('smb_shares', 'update'): {
        'build': _smb_update_build, 'execute': _smb_update_execute,
        'verify': _verify_smb_updated,
    },
    ('smb_shares', 'delete'): {
        'build': _smb_delete_build, 'execute': _smb_delete_execute,
        'verify': _verify_smb_deleted,
    },
    ('nfs_shares', 'create'): {
        'build': _nfs_create_build, 'execute': _nfs_create_execute,
        'verify': _verify_nfs_created,
    },
    ('nfs_shares', 'update'): {
        'build': _nfs_update_build, 'execute': _nfs_update_execute,
        'verify': _verify_nfs_updated,
    },
    ('nfs_shares', 'delete'): {
        'build': _nfs_delete_build, 'execute': _nfs_delete_execute,
        'verify': _verify_nfs_deleted,
    },
}


def _params_hash(params):
    """Short, stable hash of the JSON-RPC params for compact audit entries
    — the raw payload can carry dataset properties/quotas that don't
    belong bloating the audit log, but a hash still lets an operator
    correlate 'this exact call' across the dry-run preview and the audit
    trail."""
    encoded = json.dumps(params, sort_keys=True, default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()[:12]


def _resolve_writable_instance(instance_id):
    """Like ``_resolve_instance``, but for the write path: the instance
    must exist, must NOT be in readonly mode, and must have ``api_key_rw``
    configured — ALL checked before any envelope is even built, let alone
    before touching TrueNAS. ``readonly`` is the server-side kill-switch
    (brief §3) and is the final authority no matter what the UI shows.

    Returns ``(instance, error_response_or_None, reject_reason_or_None)`` —
    the reason is a short machine-readable code the caller audits even on
    a pre-execution rejection (a rejected delete attempt against a
    readonly instance is exactly the kind of signal an audit trail exists
    to catch).

    Fail-closed on ``readonly``: ``inst.get('readonly') is not False``
    treats anything other than an explicit ``false`` — missing key,
    ``true``, or a hand-edited ``null`` in config.json — as readonly.
    ``inst.get('readonly', True)`` (the previous check) only defaulted a
    MISSING key to safe; an explicit ``null`` (falsy in Python) slipped
    through as "not readonly", a real gap for a hand-edited config.json.
    """
    if not instance_id:
        return None, (jsonify({'error': 'instance_id is required'}), 400), 'missing_instance_id'
    cfg = config_store.load_config(CONFIG_PATH)
    inst = config_store.find_instance(cfg['instances'], instance_id)
    if not inst:
        return None, (jsonify({'error': f"instance '{instance_id}' not found"}), 404), 'not_found'
    if inst.get('readonly') is not False:
        return None, (jsonify({
            'error': f"instance '{instance_id}' is in readonly mode — writes "
                     f"are disabled server-side"}), 403), 'readonly'
    if not inst.get('api_key_rw'):
        return None, (jsonify({
            'error': f"instance '{instance_id}' has no api_key_rw configured — "
                     f"writes are disabled"}), 403), 'no_api_key_rw'
    return inst, None, None


def _get_rw_authenticated_connection(inst):
    """Mirrors ``_get_authenticated_connection`` but against the SEPARATE
    RW-privileged connection (``conn_manager.get_rw_connection``) — writes
    must never upgrade the shared read connection's privilege level."""
    conn = conn_manager.get_rw_connection(inst)
    if conn.needs_auth:
        raise TrueNASAuthError('auth.login_with_api_key', {
            'message': 'RW API key was rejected on a previous attempt — '
                       'check/rotate it in Settings before retrying'})
    if not conn.is_authenticated:
        conn.login(inst['api_key_rw'])
    return conn


def writes_dry_run_handler():
    """POST body: ``{subsystem, op, payload}``. Returns ``{method, params}``
    WITHOUT ever touching TrueNAS or even resolving a connection — the
    builder functions take no ``conn`` argument at all."""
    if (err := _require_admin()):
        return err
    raw_body = request.get_json(silent=True)
    if raw_body is None:
        return jsonify({'error': 'request body must be valid JSON'}), 400
    body = raw_body if isinstance(raw_body, dict) else {}
    subsystem = str(body.get('subsystem') or '')
    op = str(body.get('op') or '')
    payload = body.get('payload') or {}

    op_entry = WRITE_OPS.get((subsystem, op))
    if not op_entry:
        return jsonify({'error': f"unknown write operation '{subsystem}.{op}'"}), 400

    try:
        method, params = op_entry['build'](payload)
    except ConfirmationRequired as e:
        return jsonify({'error': str(e)}), 400
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'method': method, 'params': params})


def writes_execute_handler():
    """POST body: ``{instance_id, subsystem, op, payload}``. Runs the full
    brief §5 write flow: admin gate -> writable-instance gate (readonly +
    api_key_rw) -> build envelope (validates, incl. typed confirmation) ->
    RW connect+login -> call() -> post-write verify -> audit -> response.
    Never retries automatically on a step 5/6 failure (step 8) — the
    response always carries the real observed status so the operator can
    decide whether to re-check or retry from the UI.

    Every pre-execution rejection (unknown instance/readonly/no RW key/bad
    confirmation) is ALSO audited (as ``<action>.rejected``) — a rejected
    delete attempt is exactly the signal an audit trail exists to catch
    (F2 review round 2 finding); only ``instance_id``/unknown-op are too
    generic to attribute to any instance and are not audited.

    Once ``execute`` has actually run against TrueNAS, ``_audit()`` for the
    real outcome is guaranteed via ``try/finally`` — structurally, not by
    convention — so an exception during the post-write verify (ANY
    exception, not only ``TrueNASError``) can never leave a real write
    without an audit trail (F2 review round 2, P0)."""
    if (err := _require_admin()):
        return err
    raw_body = request.get_json(silent=True)
    if raw_body is None:
        return jsonify({'error': 'request body must be valid JSON'}), 400
    body = raw_body if isinstance(raw_body, dict) else {}
    instance_id = str(body.get('instance_id') or '').strip()
    subsystem = str(body.get('subsystem') or '')
    op = str(body.get('op') or '')
    payload = body.get('payload') or {}

    op_entry = WRITE_OPS.get((subsystem, op))
    if not op_entry:
        return jsonify({'error': f"unknown write operation '{subsystem}.{op}'"}), 400

    def _audit_rejected(reason, extra=''):
        log_audit(user=_username(), action=f'truenas.{subsystem}.{op}.rejected',
                  details=f"instance={instance_id} reason={reason}{extra}")

    inst, err_resp, reject_reason = _resolve_writable_instance(instance_id)
    if err_resp:
        _audit_rejected(reject_reason)
        return err_resp

    try:
        method, params = op_entry['build'](payload)
    except ConfirmationRequired as e:
        _audit_rejected('confirmation_mismatch', f': {e}')
        return jsonify({'error': str(e)}), 400
    except ValueError as e:
        _audit_rejected('invalid_payload', f': {e}')
        return jsonify({'error': str(e)}), 400

    client_id = inst.get('client_id', 'unassigned')
    params_hash = _params_hash(params)

    def _audit(result_status, extra=''):
        log_audit(user=_username(), action=f'truenas.{subsystem}.{op}',
                  details=(f"instance={instance_id} client={client_id} method={method} "
                           f"params_hash={params_hash} result={result_status}{extra}"))

    try:
        conn = _get_rw_authenticated_connection(inst)
        result = op_entry['execute'](conn, payload)
    except TrueNASError as e:
        log.warning(f"[{PLUGIN_ID}] write '{subsystem}.{op}' failed for "
                    f"instance '{instance_id}': {e}")
        _audit('error', f': {e}')
        return jsonify({'ok': False, 'status': 'error', 'error': str(e),
                        'method': method, 'params': params}), 502
    except Exception as e:  # defensive: never let a write bug 500 mute
        log.error(f"[{PLUGIN_ID}] unexpected error executing '{subsystem}.{op}' for "
                  f"instance '{instance_id}': {e}", exc_info=True)
        _audit('unexpected_error', f': {e}')
        return jsonify({'ok': False, 'status': 'error', 'error': f'unexpected error: {e}',
                        'method': method, 'params': params}), 500

    # bool is a subclass of int in Python — isinstance(True, int) is True.
    # A synchronous write returning True (success, no job) must never be
    # mistaken for a job id, or a real verify mismatch on it would get
    # reported as 'pending' ("still running, check later") forever instead
    # of 'verify_failed' (a real problem) (F2 review round 2 finding).
    job_id = result if isinstance(result, int) and not isinstance(result, bool) else None

    # Post-write verify (brief §5 step 6). The execute call above ALREADY
    # succeeded against TrueNAS by this point — from here on, _audit() for
    # whatever we end up reporting is GUARANTEED via try/finally, not just
    # "called at the end of the happy path". A verify that raises ANY
    # exception (not just TrueNASError — an unexpected shape/AttributeError
    # counts too) must never let a real write escape without an audit
    # entry (F2 review round 2, P0).
    verify_ok, verify_resource, verify_error = None, None, None
    status = 'verify_error'
    try:
        try:
            verify_ok, verify_resource = op_entry['verify'](conn, payload, result)
        except TrueNASError as e:
            verify_error = str(e)
            log.warning(f"[{PLUGIN_ID}] post-write verify failed for '{subsystem}.{op}' on "
                        f"instance '{instance_id}': {e}")
        except Exception as e:
            verify_error = f'unexpected error during verify: {e}'
            log.error(f"[{PLUGIN_ID}] unexpected error verifying '{subsystem}.{op}' on "
                      f"instance '{instance_id}': {e}", exc_info=True)

        if verify_error is not None:
            # Distinct from 'verify_failed': the write may well have
            # succeeded — we just couldn't confirm it (timeout, dropped
            # connection right after the write). For a delete, "still
            # exists" (verify_failed) and "couldn't check" (verify_error)
            # call for opposite operator reactions and must not collapse
            # into the same status (F2 review round 2 finding).
            status = 'verify_error'
        elif verify_ok is True:
            status = 'ok'
        elif job_id is not None:
            # Genuinely unknown whether this is an async job still running
            # or an actual failure — no job poller in F2. Report 'pending',
            # never a false success or failure (step 8).
            status = 'pending'
        else:
            status = 'verify_failed'
    finally:
        _audit(status, f' verify_error={verify_error}' if verify_error else '')

    return jsonify({
        'ok': status == 'ok',
        'status': status,
        'method': method,
        'params': params,
        'job_id': job_id,
        'verify': verify_resource,
        'verify_error': verify_error,
    })


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

ROUTES = {
    'ui': ui_handler,
    'config': config_handler,
    'config/save': config_save_handler,
    'instances/test': instances_test_handler,
    'system': system_handler,
    'pools': pools_handler,
    'datasets': datasets_handler,
    'snapshots': snapshots_handler,
    'shares': shares_handler,
    'replication': replication_handler,
    'apps_vms': apps_vms_handler,
    'services': services_handler,
    'data_protection': data_protection_handler,
    'telemetry': telemetry_handler,
    'fleet': fleet_handler,
    'writes/dry-run': writes_dry_run_handler,
    'writes/execute': writes_execute_handler,
}
