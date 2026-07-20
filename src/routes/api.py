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
       shares|replication|apps_vms         -> subsystem read (F1)     (storage.view)

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

import logging
import os

from flask import request, jsonify, send_file

from pegaprox.utils.auth import load_users
from pegaprox.utils.rbac import has_permission
from pegaprox.utils.audit import log_audit

from core.conn_manager import ConnectionManager
from core.errors import TrueNASError
from subsystems.apps_vms import apps_vms as apps_vms_subsystem
from subsystems.datasets import datasets as datasets_subsystem
from subsystems.pools import list_disks as pools_list_disks
from subsystems.pools import pools as pools_subsystem
from subsystems.pools import temperatures as pools_temperatures
from subsystems.replication import replication as replication_subsystem
from subsystems.shares import shares as shares_subsystem
from subsystems.snapshots import list_tasks as snapshots_list_tasks
from subsystems.snapshots import snapshots as snapshots_subsystem
from subsystems.system import alerts as system_alerts
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
    ``readonly`` flag, which gates writers, not this)."""
    conn = conn_manager.get_connection(inst)
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
        return jsonify({'error': str(e), 'instance_id': instance_id}), 502
    except Exception as e:  # defensive: never let a subsystem bug 500 mute
        log.error(f"[{PLUGIN_ID}] unexpected error in subsystem route for "
                  f"instance '{instance_id}': {e}", exc_info=True)
        return jsonify({'error': f'unexpected error: {e}', 'instance_id': instance_id}), 500
    return jsonify({'instance_id': instance_id, 'data': data})


def _system_fetch(conn):
    active_alerts = system_alerts(conn)
    return {
        'info': system_subsystem.read(conn),
        'alerts': active_alerts,
        'update_status': system_update_status(conn),
        'health': system_subsystem.health(conn, active_alerts=active_alerts).to_dict(),
    }


def system_handler():
    return _subsystem_route(_system_fetch)


def _pools_fetch(conn):
    pool_list = pools_subsystem.list(conn)
    disks = pools_list_disks(conn)
    return {
        'pools': pool_list,
        'disks': disks,
        'temperatures': pools_temperatures(conn, disks=disks, pools=pool_list),
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
}
