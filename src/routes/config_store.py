# -*- coding: utf-8 -*-
"""``config.json`` load/save/masking/validation for the TrueNAS plugin.

Pure functions (no Flask) so they're unit-testable in isolation — same
split as ``core/``. Mirrors the config.json + ``***`` masking pattern
verified in production by ``pegaprox-plugin-wake-on-lan``: GET always masks
``api_key_ro``/``api_key_rw``; a ``config/save`` that receives ``"***"``
unchanged must NOT clobber the previously stored key with an empty value.

Multi-tenant (brief §3.1, 2026-07-20 adjustment): every instance carries a
free-form ``client_id`` (e.g. ``"idkmanager"``, ``"acme"``, ``"globex"``,
``"initech"``) so the plugin can eventually host TrueNAS instances that
belong to different clients in the same PegaProx panel. It is NOT sensitive
— never masked, always shown in clear so the UI can group by client and
writers (F2+) can display it prominently in confirmation dialogs. F0 only
needs the field to exist, persist, and be usable for UI grouping; the real
``check_cluster_access`` gate per client is F1+.
"""

import json
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger('plugin.truenas.config_store')

DEFAULT_POLL = {'fast_s': 10, 'slow_s': 60, 'cold_s': 900}
DEFAULT_THRESHOLDS = {'warn_pct': 80, 'crit_pct': 90}
DEFAULT_NOTIFY = {'webhook_url': None}
MASK = '***'
_KEY_FIELDS = ('api_key_ro', 'api_key_rw')

# F3 (2026-07-21): api_key_ro/api_key_rw are encrypted at rest — config.json
# used to hold them in clear text with chmod 600 as the only protection.
# Versioned prefix so a future format change (enc:v2:) never gets confused
# with v1 tokens, and so a value WITHOUT the prefix is unambiguously legacy
# plaintext from before this existed (migrated transparently on next save,
# never a hard cutover that could brick an existing deploy).
ENC_PREFIX = 'enc:v1:'
_SECRET_KEY_FILENAME = 'secret.key'


def default_config():
    return {
        'instances': [], 'poll': dict(DEFAULT_POLL),
        'thresholds': dict(DEFAULT_THRESHOLDS), 'notify': dict(DEFAULT_NOTIFY),
    }


def _load_or_create_fernet(config_path):
    """Return a ``Fernet`` built from ``secret.key`` next to ``config_path``,
    generating it on first use. MUST read an existing key rather than ever
    regenerating one — every previously-encrypted api_key_ro/api_key_rw
    becomes permanently undecryptable the moment the key changes, so this
    only ever writes the file when it doesn't already exist."""
    key_path = os.path.join(os.path.dirname(config_path) or '.', _SECRET_KEY_FILENAME)
    try:
        with open(key_path, 'rb') as f:
            key = f.read().strip()
    except FileNotFoundError:
        key = Fernet.generate_key()
        tmp = key_path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(key)
        os.replace(tmp, key_path)
        try:
            os.chmod(key_path, 0o600)
        except OSError as e:
            log.warning(f'[truenas] could not chmod 600 {key_path!r}: {e}')
        log.info(f'[truenas] generated a new encryption key at {key_path!r}')
    return Fernet(key)


def encrypt_value(value, fernet):
    """Encrypt a plaintext API key for storage. Falsy values (None/"") pass
    through unchanged — "no key configured" must stay indistinguishable
    from itself before/after this feature, never become an encrypted empty
    string that then fails to decrypt back to ''."""
    if not value:
        return value
    return ENC_PREFIX + fernet.encrypt(value.encode()).decode()


def decrypt_value(stored, fernet, label):
    """Decrypt a stored field back to plaintext for in-memory use. A value
    with no ``enc:v1:`` prefix is legacy plaintext from before this existed
    — passed through as-is, transparently re-encrypted on the next save
    (see ``save_config``). A value that HAS the prefix but fails to decrypt
    (corrupt ciphertext, or secret.key was lost/replaced) must never crash
    the whole config load or silently return garbage — logged loudly and
    treated as unset, same recovery path as any other "no key configured"
    state: the operator re-pastes it from Settings."""
    if not stored or not stored.startswith(ENC_PREFIX):
        return stored
    token = stored[len(ENC_PREFIX):]
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        log.error(f"[truenas] {label}: stored value is encrypted but failed to decrypt "
                  f"(corrupt ciphertext, or secret.key changed) — treating as unset; "
                  f"the operator must re-enter it from Settings")
        return None


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
    fernet = _load_or_create_fernet(path)
    cfg['instances'] = [
        dict(inst, **{
            f: decrypt_value(inst.get(f), fernet, f"instance '{inst.get('id', '?')}'.{f}")
            for f in _KEY_FIELDS
        })
        for inst in cfg['instances']
    ]
    poll = dict(DEFAULT_POLL)
    poll.update(cfg.get('poll') or {})
    cfg['poll'] = poll
    thresholds = dict(DEFAULT_THRESHOLDS)
    thresholds.update(cfg.get('thresholds') or {})
    cfg['thresholds'] = thresholds
    notify = dict(DEFAULT_NOTIFY)
    notify.update(cfg.get('notify') or {})
    cfg['notify'] = notify
    return cfg


def save_config(path, cfg):
    """Atomic write (tmp + os.replace) + chmod 600, same pattern as
    wake-on-lan's ``_save_config``. Encrypts api_key_ro/api_key_rw before
    writing (F3) — callers always work with plaintext in memory; only the
    on-disk copy is encrypted. Builds a NEW instances list for the on-disk
    version rather than mutating ``cfg`` in place, since callers keep using
    the same ``cfg``/instance dicts (in plaintext) after this returns."""
    fernet = _load_or_create_fernet(path)
    on_disk = dict(cfg)
    on_disk['instances'] = [
        dict(inst, **{f: encrypt_value(inst.get(f), fernet) for f in _KEY_FIELDS})
        for inst in cfg.get('instances', [])
    ]
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(on_disk, f, indent=2)
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


def validate_instances(raw_instances, old_instances, global_thresholds=None):
    """Validate + round-trip masked keys. Returns (clean_list, error_or_None).

    Enforces the brief's hard safety rule: ``use_tls`` must be true whenever
    either API key is (or will remain) set — TrueNAS auto-revokes a key used
    over plain HTTP, so shipping a config that pairs a real key with
    ``use_tls: false`` is a footgun the plugin refuses to save.

    ``global_thresholds`` (defaults to ``DEFAULT_THRESHOLDS`` when omitted,
    so existing call sites that pre-date per-instance thresholds keep
    working unchanged) is the ALREADY-VALIDATED global warn/crit pair — an
    instance's optional ``warn_pct``/``crit_pct`` override is checked against
    the EFFECTIVE pair (override-or-global for each side independently),
    not against itself in isolation, so e.g. overriding only ``warn_pct``
    to a value above the global ``crit_pct`` is still rejected.
    """
    if not isinstance(raw_instances, list):
        return None, 'instances must be a list'
    global_thresholds = global_thresholds or DEFAULT_THRESHOLDS

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

        inst_warn = inst_crit = None
        for field, attr in (('warn_pct', 'inst_warn'), ('crit_pct', 'inst_crit')):
            raw_value = raw.get(field)
            if raw_value is None or raw_value == '':
                continue
            try:
                parsed = int(raw_value)
            except (TypeError, ValueError):
                return None, f"instance '{inst_id}': {field} must be an integer"
            if not (1 <= parsed <= 99):
                return None, f"instance '{inst_id}': {field} must be between 1 and 99"
            if attr == 'inst_warn':
                inst_warn = parsed
            else:
                inst_crit = parsed
        effective_warn = inst_warn if inst_warn is not None else global_thresholds['warn_pct']
        effective_crit = inst_crit if inst_crit is not None else global_thresholds['crit_pct']
        if effective_warn >= effective_crit:
            return None, (
                f"instance '{inst_id}': warn_pct must be less than crit_pct "
                f"(effective {effective_warn} >= {effective_crit})"
            )

        clean.append({
            'id': inst_id,
            'name': str(raw.get('name') or inst_id),
            'client_id': str(raw.get('client_id') or '').strip() or 'unassigned',
            'host': host,
            'port': port,
            'use_tls': use_tls,
            'verify_tls': bool(raw.get('verify_tls', False)),
            # Overrides TLS/SNI hostname verification independently of
            # `host` — real TrueNAS instances are commonly reached by LAN
            # IP but present a CA-issued cert bound to a DNS name (e.g. an
            # ACME cert for remote access). Not a secret: no masking needed.
            'tls_server_name': (str(raw.get('tls_server_name')).strip()
                                 if raw.get('tls_server_name') else None),
            'api_key_ro': keys['api_key_ro'],
            'api_key_rw': keys['api_key_rw'],
            'readonly': bool(raw.get('readonly', True)),
            'warn_pct': inst_warn,
            'crit_pct': inst_crit,
        })
    return clean, None


def validate_thresholds(raw):
    """Validate the global alert-threshold pair (brief for the F2
    configurable-thresholds workstream). Mirrors ``validate_poll``'s shape:
    a missing/partial dict fills in from ``DEFAULT_THRESHOLDS``, an invalid
    one is rejected outright rather than silently clamped."""
    thresholds = dict(DEFAULT_THRESHOLDS)
    if raw is None:
        return thresholds, None
    if not isinstance(raw, dict):
        return None, 'thresholds must be an object'
    for key in ('warn_pct', 'crit_pct'):
        if key in raw:
            try:
                value = int(raw[key])
            except (TypeError, ValueError):
                return None, f'thresholds.{key} must be an integer'
            if not (1 <= value <= 99):
                return None, f'thresholds.{key} must be between 1 and 99'
            thresholds[key] = value
    if thresholds['warn_pct'] >= thresholds['crit_pct']:
        return None, 'thresholds.warn_pct must be less than thresholds.crit_pct'
    return thresholds, None


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


def mask_notify(notify):
    """Same masking convention as api_key_ro/rw — a webhook URL commonly
    embeds a bearer token in its path/query (Slack/Discord/Teams webhooks
    all work this way), so it's treated as a secret too: masked on GET,
    round-tripped on save when the masked sentinel comes back unchanged."""
    safe = dict(notify)
    if safe.get('webhook_url'):
        safe['webhook_url'] = MASK
    return safe


def validate_notify(raw, old_notify=None):
    """Validate the F4a notification-channel config. A ``webhook_url`` of
    ``None``/``""`` means "no webhook configured" — the poller simply skips
    delivery, this is not an error (matches api_key_ro/rw's own "falsy is a
    legitimate unset state" convention). A non-empty value must at least
    look like an http(s) URL — a typo here would otherwise fail silently
    forever inside the poller thread instead of at save time, where the
    operator can see it immediately."""
    old_notify = old_notify or DEFAULT_NOTIFY
    notify = dict(DEFAULT_NOTIFY)
    if raw is None:
        return notify, None
    if not isinstance(raw, dict):
        return None, 'notify must be an object'
    url = raw.get('webhook_url')
    if url == MASK:
        if not old_notify.get('webhook_url'):
            return None, ('notify.webhook_url: se recibió enmascarado pero no hay un valor '
                          'previo guardado (volvé a pegar la URL)')
        notify['webhook_url'] = old_notify['webhook_url']
    elif url:
        if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            return None, 'notify.webhook_url must start with http:// or https://'
        notify['webhook_url'] = url
    return notify, None
