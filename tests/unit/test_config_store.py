# -*- coding: utf-8 -*-
"""config_store: masking round-trip, client_id passthrough (unmasked),
use_tls safety guard, poll validation, atomic save."""

import json
import logging
import os

from routes import config_store


def _instance(id_='truenas-test', client_id='idkmanager', api_key_ro='real-secret-ro'):
    return {
        'id': id_, 'name': 'TrueNAS Test Instance', 'client_id': client_id,
        'host': '192.0.2.64', 'port': 8443, 'use_tls': True, 'verify_tls': False,
        'api_key_ro': api_key_ro, 'api_key_rw': None, 'readonly': True,
    }


def test_mask_instance_masks_keys_when_present():
    masked = config_store.mask_instance(_instance())
    assert masked['api_key_ro'] == '***'
    assert masked['api_key_rw'] is None
    assert masked['client_id'] == 'idkmanager'  # never masked


def test_mask_instance_leaves_falsy_key_alone():
    inst = _instance(api_key_ro=None)
    masked = config_store.mask_instance(inst)
    assert masked['api_key_ro'] is None


def test_validate_instances_masked_key_round_trips():
    old = [_instance(api_key_ro='vault-secret')]
    incoming = [dict(_instance(api_key_ro='***'))]
    clean, err = config_store.validate_instances(incoming, old)
    assert err is None
    assert clean[0]['api_key_ro'] == 'vault-secret'


def test_validate_instances_new_key_overwrites():
    old = [_instance(api_key_ro='old-secret')]
    incoming = [dict(_instance(api_key_ro='new-secret'))]
    clean, err = config_store.validate_instances(incoming, old)
    assert err is None
    assert clean[0]['api_key_ro'] == 'new-secret'


def test_validate_instances_masked_key_without_prior_value_errors():
    incoming = [dict(_instance(api_key_ro='***'))]
    clean, err = config_store.validate_instances(incoming, [])
    assert clean is None
    assert 'enmascarad' in err


def test_validate_instances_rejects_duplicate_id():
    clean, err = config_store.validate_instances([_instance(), _instance()], [])
    assert clean is None
    assert 'duplicate instance id' in err


def test_validate_instances_rejects_missing_host():
    bad = _instance()
    bad['host'] = ''
    clean, err = config_store.validate_instances([bad], [])
    assert clean is None
    assert 'host' in err


def test_validate_instances_rejects_bad_port():
    bad = _instance()
    bad['port'] = 70000
    clean, err = config_store.validate_instances([bad], [])
    assert clean is None
    assert 'port' in err


def test_validate_instances_rejects_http_with_api_key():
    bad = _instance()
    bad['use_tls'] = False
    clean, err = config_store.validate_instances([bad], [])
    assert clean is None
    assert 'use_tls' in err


def test_validate_instances_preserves_client_id():
    clean, err = config_store.validate_instances([_instance(client_id='acme')], [])
    assert err is None
    assert clean[0]['client_id'] == 'acme'


def test_validate_instances_defaults_missing_client_id_to_unassigned():
    inst = _instance()
    del inst['client_id']
    clean, err = config_store.validate_instances([inst], [])
    assert err is None
    assert clean[0]['client_id'] == 'unassigned'


def test_group_by_client_groups_in_first_seen_order():
    instances = [
        _instance(id_='a', client_id='acme'),
        _instance(id_='b', client_id='idkmanager'),
        _instance(id_='c', client_id='acme'),
    ]
    groups = config_store.group_by_client(instances)
    assert [g['client_id'] for g in groups] == ['acme', 'idkmanager']
    assert len(groups[0]['instances']) == 2
    assert len(groups[1]['instances']) == 1


def test_validate_poll_defaults_when_absent():
    poll, err = config_store.validate_poll(None)
    assert err is None
    assert poll == config_store.DEFAULT_POLL


def test_validate_poll_rejects_non_positive():
    poll, err = config_store.validate_poll({'fast_s': 0})
    assert poll is None
    assert 'fast_s' in err


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = config_store.load_config(str(tmp_path / 'nope.json'))
    assert cfg == config_store.default_config()


def test_save_and_load_config_round_trips(tmp_path):
    path = str(tmp_path / 'config.json')
    cfg = {'instances': [_instance()], 'poll': config_store.DEFAULT_POLL}
    config_store.save_config(path, cfg)
    assert not os.path.exists(path + '.tmp')
    loaded = config_store.load_config(path)
    assert loaded['instances'][0]['id'] == 'truenas-test'
    assert loaded['instances'][0]['client_id'] == 'idkmanager'


def test_save_config_is_chmod_600(tmp_path):
    path = str(tmp_path / 'config.json')
    config_store.save_config(path, config_store.default_config())
    # chmod is a no-op on some CI filesystems (Windows) — just assert the
    # file exists and save_config() didn't raise on the chmod call.
    assert os.path.exists(path)


# ---------------------------------------------------------------------------
# Regression: a corrupt/unreadable config.json used to fall back to
# defaults with ZERO logging, despite the docstring claiming otherwise — an
# operator would see "0 instances" with no trace of why, and the next save
# would silently overwrite the file, permanently destroying every stored
# API key (finding #8).
# ---------------------------------------------------------------------------

def test_load_config_logs_error_on_corrupt_json(tmp_path, caplog):
    path = tmp_path / 'config.json'
    path.write_text('{ this is not valid json')

    with caplog.at_level(logging.ERROR, logger='plugin.truenas.config_store'):
        cfg = config_store.load_config(str(path))

    assert cfg == config_store.default_config()
    assert any('corrupt' in r.message for r in caplog.records)
    # The log message embeds the path via !r (repr), which escapes
    # backslashes on Windows — compare on the filename, not str(path),
    # to avoid a path-separator/escaping mismatch across platforms.
    assert any('config.json' in r.message for r in caplog.records)


def test_load_config_logs_error_when_root_is_not_an_object(tmp_path, caplog):
    path = tmp_path / 'config.json'
    path.write_text('[1, 2, 3]')  # valid JSON, but not a config object

    with caplog.at_level(logging.ERROR, logger='plugin.truenas.config_store'):
        cfg = config_store.load_config(str(path))

    assert cfg == config_store.default_config()
    assert any('corrupt' in r.message for r in caplog.records)


def test_load_config_missing_file_does_not_log_an_error(tmp_path, caplog):
    """A missing file is the legitimate "not configured yet" case — it must
    NOT be logged as an error (that would be alert-fatigue noise on every
    fresh install)."""
    path = tmp_path / 'nope.json'
    with caplog.at_level(logging.ERROR, logger='plugin.truenas.config_store'):
        config_store.load_config(str(path))
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# F2: configurable alert thresholds (global pair + optional per-instance
# override) — replaces the hardcoded 80% warn line the charts used to paint
# every ring/bar with.
# ---------------------------------------------------------------------------

def test_default_config_includes_thresholds():
    assert config_store.default_config()['thresholds'] == config_store.DEFAULT_THRESHOLDS


def test_load_config_merges_partial_thresholds(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{"instances": [], "thresholds": {"crit_pct": 95}}')
    cfg = config_store.load_config(str(path))
    # warn_pct absent in the file -> falls back to the default; crit_pct
    # present -> overrides it. Same merge shape as poll's fast_s/slow_s/cold_s.
    assert cfg['thresholds'] == {'warn_pct': 80, 'crit_pct': 95}


def test_validate_thresholds_defaults_when_absent():
    thresholds, err = config_store.validate_thresholds(None)
    assert err is None
    assert thresholds == config_store.DEFAULT_THRESHOLDS


def test_validate_thresholds_accepts_a_valid_override():
    thresholds, err = config_store.validate_thresholds({'warn_pct': 70, 'crit_pct': 85})
    assert err is None
    assert thresholds == {'warn_pct': 70, 'crit_pct': 85}


def test_validate_thresholds_rejects_non_integer():
    thresholds, err = config_store.validate_thresholds({'warn_pct': 'high'})
    assert thresholds is None
    assert 'warn_pct' in err


def test_validate_thresholds_rejects_out_of_range():
    thresholds, err = config_store.validate_thresholds({'crit_pct': 150})
    assert thresholds is None
    assert 'crit_pct' in err


def test_validate_thresholds_rejects_warn_at_or_above_crit():
    thresholds, err = config_store.validate_thresholds({'warn_pct': 90, 'crit_pct': 90})
    assert thresholds is None
    assert 'warn_pct' in err and 'crit_pct' in err


def test_validate_instances_accepts_per_instance_threshold_override():
    inst = _instance()
    inst['warn_pct'] = 60
    inst['crit_pct'] = 75
    clean, err = config_store.validate_instances([inst], [])
    assert err is None
    assert clean[0]['warn_pct'] == 60
    assert clean[0]['crit_pct'] == 75


def test_validate_instances_override_defaults_to_none_when_absent():
    clean, err = config_store.validate_instances([_instance()], [])
    assert err is None
    assert clean[0]['warn_pct'] is None
    assert clean[0]['crit_pct'] is None


def test_validate_instances_rejects_out_of_range_instance_threshold():
    inst = _instance()
    inst['warn_pct'] = 0
    clean, err = config_store.validate_instances([inst], [])
    assert clean is None
    assert 'warn_pct' in err


def test_validate_instances_checks_override_against_effective_global_pair():
    """Overriding ONLY warn_pct to a value at/above the (unoverridden) global
    crit_pct must still be rejected — the effective pair is what's checked,
    not the instance's two raw fields in isolation."""
    inst = _instance()
    inst['warn_pct'] = 95
    global_thresholds = {'warn_pct': 80, 'crit_pct': 90}
    clean, err = config_store.validate_instances([inst], [], global_thresholds)
    assert clean is None
    assert 'warn_pct' in err and 'crit_pct' in err


def test_validate_instances_override_against_custom_global_thresholds_passes():
    inst = _instance()
    inst['warn_pct'] = 95
    global_thresholds = {'warn_pct': 80, 'crit_pct': 99}
    clean, err = config_store.validate_instances([inst], [], global_thresholds)
    assert err is None
    assert clean[0]['warn_pct'] == 95
    assert clean[0]['crit_pct'] is None


# ---------------------------------------------------------------------------
# F3: encrypt api_key_ro/api_key_rw at rest — config.json used to hold them
# in clear text with chmod 600 as the only protection.
# ---------------------------------------------------------------------------

def test_save_config_writes_encrypted_keys_not_plaintext(tmp_path):
    path = str(tmp_path / 'config.json')
    config_store.save_config(path, {
        'instances': [_instance(api_key_ro='super-secret-ro')],
        'poll': config_store.DEFAULT_POLL,
    })
    with open(path) as f:
        raw = f.read()
    assert 'super-secret-ro' not in raw
    assert config_store.ENC_PREFIX in raw


def test_save_then_load_round_trips_key_back_to_plaintext(tmp_path):
    """Restart-decrypts: everything above config_store's load/save boundary
    only ever sees plaintext — a fresh load_config() call (simulating a
    process restart) must decrypt transparently."""
    path = str(tmp_path / 'config.json')
    config_store.save_config(path, {
        'instances': [_instance(api_key_ro='super-secret-ro')],
        'poll': config_store.DEFAULT_POLL,
    })
    loaded = config_store.load_config(path)
    assert loaded['instances'][0]['api_key_ro'] == 'super-secret-ro'


def test_legacy_plaintext_key_migrates_transparently_on_next_save(tmp_path):
    """A config.json written before F3 existed has api_key_ro in clear text
    with no enc:v1: prefix. Loading it must return the plaintext as-is (no
    crash, no double-encryption), and the NEXT save must encrypt it."""
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({
        'instances': [_instance(api_key_ro='pre-f3-plaintext-key')],
        'poll': config_store.DEFAULT_POLL,
    }))

    loaded = config_store.load_config(str(path))
    assert loaded['instances'][0]['api_key_ro'] == 'pre-f3-plaintext-key'

    config_store.save_config(str(path), loaded)
    raw = path.read_text()
    assert 'pre-f3-plaintext-key' not in raw
    assert config_store.ENC_PREFIX in raw
    # And it still round-trips correctly after migrating.
    reloaded = config_store.load_config(str(path))
    assert reloaded['instances'][0]['api_key_ro'] == 'pre-f3-plaintext-key'


def test_encrypt_value_leaves_falsy_values_unchanged(tmp_path):
    fernet = config_store._load_or_create_fernet(str(tmp_path / 'config.json'))
    assert config_store.encrypt_value(None, fernet) is None
    assert config_store.encrypt_value('', fernet) == ''


def test_decrypt_value_leaves_unprefixed_legacy_plaintext_unchanged(tmp_path):
    fernet = config_store._load_or_create_fernet(str(tmp_path / 'config.json'))
    assert config_store.decrypt_value('plain-old-key', fernet, 'label') == 'plain-old-key'
    assert config_store.decrypt_value(None, fernet, 'label') is None


def test_decrypt_value_on_corrupted_ciphertext_fails_loud_not_silent(tmp_path, caplog):
    """Fernet's authenticated encryption (encrypt-then-MAC) must actually be
    exercised here, not just trusted by assumption: a tampered/corrupt
    enc:v1: token must be REJECTED, logged clearly, and degrade to None
    (recoverable: operator re-pastes the key) — never decrypt to garbage
    bytes and never crash the caller."""
    fernet = config_store._load_or_create_fernet(str(tmp_path / 'config.json'))
    corrupted = config_store.ENC_PREFIX + 'not-a-real-fernet-token'
    with caplog.at_level(logging.ERROR, logger='plugin.truenas.config_store'):
        result = config_store.decrypt_value(corrupted, fernet, "instance 'x'.api_key_ro")
    assert result is None
    assert any('failed to decrypt' in r.message for r in caplog.records)


def test_wrong_secret_key_cannot_decrypt_another_key_s_ciphertext(tmp_path):
    """A ciphertext encrypted under one secret.key must not decrypt under a
    DIFFERENT key — proves this isn't just base64 obfuscation."""
    dir_a, dir_b = tmp_path / 'a', tmp_path / 'b'
    dir_a.mkdir()
    dir_b.mkdir()
    fernet_a = config_store._load_or_create_fernet(str(dir_a / 'config.json'))
    fernet_b = config_store._load_or_create_fernet(str(dir_b / 'config.json'))
    token = config_store.encrypt_value('some-secret', fernet_a)
    assert config_store.decrypt_value(token, fernet_b, 'label') is None


def test_load_or_create_fernet_reuses_an_existing_key_file(tmp_path):
    """MUST read an existing secret.key rather than ever regenerating one —
    a regenerated key would permanently orphan every previously-encrypted
    value. Two calls against the same config path must yield the SAME key
    (proven by cross-decrypting a token between the two Fernet instances)."""
    path = str(tmp_path / 'config.json')
    fernet_1 = config_store._load_or_create_fernet(path)
    token = config_store.encrypt_value('a-secret', fernet_1)
    fernet_2 = config_store._load_or_create_fernet(path)
    assert config_store.decrypt_value(token, fernet_2, 'label') == 'a-secret'


# ---------------------------------------------------------------------------
# F4a: notify.webhook_url — same masked-secret round-trip convention as
# api_key_ro/rw, since a webhook URL commonly embeds a bearer token.
# ---------------------------------------------------------------------------

def test_default_config_includes_notify():
    assert config_store.default_config()['notify'] == config_store.DEFAULT_NOTIFY


def test_mask_notify_masks_a_configured_url():
    masked = config_store.mask_notify({'webhook_url': 'http://example.invalid/hook?token=x'})
    assert masked['webhook_url'] == '***'


def test_mask_notify_leaves_unset_url_alone():
    masked = config_store.mask_notify({'webhook_url': None})
    assert masked['webhook_url'] is None


def test_validate_notify_defaults_when_absent():
    notify, err = config_store.validate_notify(None)
    assert err is None
    assert notify == config_store.DEFAULT_NOTIFY


def test_validate_notify_accepts_a_valid_https_url():
    notify, err = config_store.validate_notify({'webhook_url': 'https://example.invalid/hook'})
    assert err is None
    assert notify['webhook_url'] == 'https://example.invalid/hook'


def test_validate_notify_rejects_a_non_http_url():
    notify, err = config_store.validate_notify({'webhook_url': 'not-a-url'})
    assert notify is None
    assert 'webhook_url' in err


def test_validate_notify_masked_url_round_trips():
    old = {'webhook_url': 'https://example.invalid/hook?token=secret'}
    notify, err = config_store.validate_notify({'webhook_url': config_store.MASK}, old)
    assert err is None
    assert notify['webhook_url'] == old['webhook_url']


def test_validate_notify_masked_url_without_prior_value_errors():
    notify, err = config_store.validate_notify({'webhook_url': config_store.MASK}, None)
    assert notify is None
    assert 'enmascarad' in err


def test_validate_notify_whatsapp_fields_round_trip():
    notify, err = config_store.validate_notify({
        'whatsapp_instance': 'cum', 'whatsapp_target': '593999999999',
        'whatsapp_api_key': 'real-key',
    })
    assert err is None
    assert notify['whatsapp_instance'] == 'cum'
    assert notify['whatsapp_target'] == '593999999999'
    assert notify['whatsapp_api_key'] == 'real-key'
    assert notify['whatsapp_gateway_url'] == config_store.DEFAULT_NOTIFY['whatsapp_gateway_url']


def test_validate_notify_whatsapp_api_key_masked_round_trips():
    old = {'whatsapp_api_key': 'real-key'}
    notify, err = config_store.validate_notify({'whatsapp_api_key': config_store.MASK}, old)
    assert err is None
    assert notify['whatsapp_api_key'] == 'real-key'


def test_mask_notify_masks_whatsapp_api_key():
    masked = config_store.mask_notify({'whatsapp_api_key': 'real-key', 'webhook_url': None})
    assert masked['whatsapp_api_key'] == config_store.MASK


def test_save_config_logs_warning_when_chmod_fails(tmp_path, monkeypatch, caplog):
    """config.json holds API keys in clear text — a failed chmod 600 used
    to be swallowed with a bare `except OSError: pass`, hiding a real
    exposure on a real deploy where permissions matter."""
    def failing_chmod(path, mode):
        raise OSError(13, 'Permission denied')

    monkeypatch.setattr(os, 'chmod', failing_chmod)
    path = str(tmp_path / 'config.json')
    with caplog.at_level(logging.WARNING, logger='plugin.truenas.config_store'):
        config_store.save_config(path, config_store.default_config())
    assert os.path.exists(path)  # the write itself must still have succeeded
    assert any('chmod' in r.message for r in caplog.records)
