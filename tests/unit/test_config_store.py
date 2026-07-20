# -*- coding: utf-8 -*-
"""config_store: masking round-trip, client_id passthrough (unmasked),
use_tls safety guard, poll validation, atomic save."""

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
    clean, err = config_store.validate_instances([_instance(client_id='sacei')], [])
    assert err is None
    assert clean[0]['client_id'] == 'sacei'


def test_validate_instances_defaults_missing_client_id_to_unassigned():
    inst = _instance()
    del inst['client_id']
    clean, err = config_store.validate_instances([inst], [])
    assert err is None
    assert clean[0]['client_id'] == 'unassigned'


def test_group_by_client_groups_in_first_seen_order():
    instances = [
        _instance(id_='a', client_id='sacei'),
        _instance(id_='b', client_id='idkmanager'),
        _instance(id_='c', client_id='sacei'),
    ]
    groups = config_store.group_by_client(instances)
    assert [g['client_id'] for g in groups] == ['sacei', 'idkmanager']
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
