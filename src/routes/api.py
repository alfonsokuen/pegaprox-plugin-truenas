# -*- coding: utf-8 -*-
"""Flask route handlers for the TrueNAS plugin, dispatched by PegaProx's
plugin catch-all under ``/api/plugins/truenas/api/<path>``.

RBAC (brief §2 — PegaProx's PERMISSIONS table is fixed, a plugin cannot
register new verbs):
  GET  ui              -> serve the UI shell               (storage.view)
  GET  config          -> instances (keys masked) + poll    (admin)
  POST config/save     -> validate + persist config         (admin)
  POST instances/test  -> connect+login_with_api_key only   (admin)

F0 exposes ONLY these routes. Every other tab in the UI is empty chrome —
no subsystem route exists yet (F1+). Reads are nominally gated by
``storage.view`` (per the brief), but config/instances-test also touch API
keys, so — mirroring wake-on-lan's SSH-credentials gate — they require the
admin role outright, not just the read verb.
"""

import logging
import os

from flask import request, jsonify, send_file

from pegaprox.utils.auth import load_users
from pegaprox.utils.rbac import has_permission
from pegaprox.utils.audit import log_audit

from core.conn_manager import ConnectionManager
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

    instance_cfg = {
        'id': instance_id or f'test-{host}',
        'host': host, 'port': port,
        'use_tls': bool(use_tls), 'verify_tls': bool(verify_tls),
    }
    result = conn_manager.test_connection(instance_cfg, api_key_ro)
    client_id = (saved or {}).get('client_id', 'unassigned')
    log_audit(user=_username(), action='truenas.instance_test',
              details=(f"instance={instance_cfg['id']} client={client_id} "
                       f"host={host} ok={result['ok']}"))
    return jsonify(result)


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

ROUTES = {
    'ui': ui_handler,
    'config': config_handler,
    'config/save': config_save_handler,
    'instances/test': instances_test_handler,
}
