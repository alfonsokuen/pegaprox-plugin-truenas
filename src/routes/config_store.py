# -*- coding: utf-8 -*-
"""``config.json`` load/save/masking/validation for the TrueNAS plugin.

Pure functions (no Flask) so they're unit-testable in isolation — same
split as ``core/``. Mirrors the config.json + ``***`` masking pattern
verified in production by ``pegaprox-plugin-wake-on-lan``: GET always masks
``api_key_ro``/``api_key_rw``; a ``config/save`` that receives ``"***"``
unchanged must NOT clobber the previously stored key with an empty value.

Multi-tenant (brief §3.1, 2026-07-20 adjustment): every instance carries a
free-form ``client_id`` (e.g. ``"idkmanager"``, ``"sacei"``, ``"ingesa"``,
``"geospace"``) so the plugin can eventually host TrueNAS instances that
belong to different clients in the same PegaProx panel. It is NOT sensitive
— never masked, always shown in clear so the UI can group by client and
writers (F2+) can display it prominently in confirmation dialogs. F0 only
needs the field to exist, persist, and be usable for UI grouping; the real
``check_cluster_access`` gate per client is F1+.
"""

import json
import logging
import os

log = logging.getLogger('plugin.truenas.config_store')

DEFAULT_POLL = {'fast_s': 10, 'slow_s': 60, 'cold_s': 900}
MASK = '***'
_KEY_FIELDS = ('api_key_ro', 'api_key_rw')


def default_config():
    return {'instances': [], 'poll': dict(DEFAULT_POLL)}


def load_config(path):
    """Load config.json. A missing file is the legitimate "not configured
    yet" case -> defaults. A corrupt/unreadable file logs and also falls
    back to defaults (same precedent as wake-on-lan's config loader) — the
    operator re-enters instances from the UI; nothing destructive happens
    since instances aren't an accumulating history like a log."""
    try:
        with open(path) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError('config root must be an object')
    except FileNotFoundError:
        return default_config()
    except Exception as e:
        # This used to be swallowed with no logging at all, despite the
        # docstring above claiming otherwise. A corrupt config.json meant
        # instances silently reverted to an empty list with zero trace of
        # why — and the very next config/save would overwrite the file,
        # permanently destroying every stored API key with no record of
        # what happened. Loud and clear now.
        log.error(f"[truenas] config at {path!r} is corrupt/unreadable, falling back to "
                  f"an empty config until the operator re-saves from the UI: {e}",
                  exc_info=True)
        return default_config()

    cfg.setdefault('instances', [])
    if not isinstance(cfg['instances'], list):
        cfg['instances'] = []
    poll = dict(DEFAULT_POLL)
    poll.update(cfg.get('poll') or {})
    cfg['poll'] = poll
    return cfg


def save_config(path, cfg):
    """Atomic write (tmp + os.replace) + chmod 600, same pattern as
    wake-on-lan's ``_save_config``."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        # This file holds API keys in clear text — a failed chmod means it
        # may be left world/group-readable. Not fatal (some filesystems,
        # e.g. Windows dev boxes, don't support POSIX permissions at all),
        # but silently swallowing it on a real deploy would hide a real
        # exposure. Warn with the errno so an operator can act on it.
        log.warning(f'[truenas] could not chmod 600 {path!r}: {e}')


def mask_instance(inst):
    """Return a copy of ``inst`` with api_key_ro/api_key_rw masked to '***'
    when a real (non-empty) value is stored; falsy values (None/"") pass
    through unmasked so the UI can tell "no key configured" apart from
    "key configured, hidden"."""
    safe = dict(inst)
    for field in _KEY_FIELDS:
        if safe.get(field):
            safe[field] = MASK
    return safe


def group_by_client(instances):
    """Group instances by ``client_id`` for the Settings/selector UI —
    returns an ordered list of ``{"client_id": ..., "instances": [...]}``,
    clients in first-seen order. Instances without a client_id land under
    the sentinel ``"unassigned"`` rather than being dropped."""
    order = []
    groups = {}
    for inst in instances:
        client_id = str(inst.get('client_id') or 'unassigned')
        if client_id not in groups:
            groups[client_id] = []
            order.append(client_id)
        groups[client_id].append(inst)
    return [{'client_id': cid, 'instances': groups[cid]} for cid in order]


def find_instance(instances, instance_id):
    for inst in instances:
        if inst.get('id') == instance_id:
            return inst
    return None


def validate_instances(raw_instances, old_instances):
    """Validate + round-trip masked keys. Returns (clean_list, error_or_None).

    Enforces the brief's hard safety rule: ``use_tls`` must be true whenever
    either API key is (or will remain) set — TrueNAS auto-revokes a key used
    over plain HTTP, so shipping a config that pairs a real key with
    ``use_tls: false`` is a footgun the plugin refuses to save.
    """
    if not isinstance(raw_instances, list):
        return None, 'instances must be a list'

    seen_ids = set()
    clean = []
    for raw in raw_instances:
        if not isinstance(raw, dict):
            return None, 'each instance must be an object'

        inst_id = str(raw.get('id') or '').strip()
        if not inst_id:
            return None, 'each instance needs an id'
        if inst_id in seen_ids:
            return None, f"duplicate instance id '{inst_id}'"
        seen_ids.add(inst_id)

        host = str(raw.get('host') or '').strip()
        if not host:
            return None, f"instance '{inst_id}': host is required"

        try:
            port = int(raw.get('port'))
        except (TypeError, ValueError):
            return None, f"instance '{inst_id}': port must be an integer"
        if not (1 <= port <= 65535):
            return None, f"instance '{inst_id}': port out of range"

        use_tls = bool(raw.get('use_tls', True))

        old = find_instance(old_instances, inst_id) or {}
        keys = {}
        for field in _KEY_FIELDS:
            incoming = raw.get(field)
            if incoming == MASK:
                if not old.get(field):
                    return None, (
                        f"instance '{inst_id}': se recibió {field} enmascarado pero no "
                        f"hay un valor previo guardado (¿lo renombraste? volvé a pegar la key)"
                    )
                keys[field] = old.get(field)
            else:
                keys[field] = incoming or None

        if not use_tls and (keys['api_key_ro'] or keys['api_key_rw']):
            return None, (
                f"instance '{inst_id}': use_tls debe ser true si hay una API key "
                f"configurada (TrueNAS revoca la key automáticamente sobre HTTP plano)"
            )

        clean.append({
            'id': inst_id,
            'name': str(raw.get('name') or inst_id),
            'client_id': str(raw.get('client_id') or '').strip() or 'unassigned',
            'host': host,
            'port': port,
            'use_tls': use_tls,
            'verify_tls': bool(raw.get('verify_tls', False)),
            'api_key_ro': keys['api_key_ro'],
            'api_key_rw': keys['api_key_rw'],
            'readonly': bool(raw.get('readonly', True)),
        })
    return clean, None


def validate_poll(raw_poll):
    """Validate the polling budget (brief §4.3). Returns (clean, error_or_None)."""
    poll = dict(DEFAULT_POLL)
    if raw_poll is None:
        return poll, None
    if not isinstance(raw_poll, dict):
        return None, 'poll must be an object'
    for key in ('fast_s', 'slow_s', 'cold_s'):
        if key in raw_poll:
            try:
                value = int(raw_poll[key])
            except (TypeError, ValueError):
                return None, f'poll.{key} must be an integer'
            if value < 1:
                return None, f'poll.{key} must be >= 1'
            poll[key] = value
    return poll, None
