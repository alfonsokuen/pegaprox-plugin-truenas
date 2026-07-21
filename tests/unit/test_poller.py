# -*- coding: utf-8 -*-
"""core/poller.py: one poll cycle never crashes the thread, double-start
guard, config is re-read fresh every cycle."""

import threading
import time

from core import poller
from routes import config_store


def _fake_get_conn(inst):
    return object()


def _instance(id_='inst-1', **overrides):
    inst = {
        'id': id_, 'name': id_, 'client_id': 'idkmanager',
        'host': '192.0.2.1', 'port': 443, 'use_tls': True, 'verify_tls': False,
        'api_key_ro': 'k', 'api_key_rw': None, 'readonly': True,
        'warn_pct': None, 'crit_pct': None,
    }
    inst.update(overrides)
    return inst


def test_run_one_cycle_with_no_instances_is_a_safe_no_op(tmp_path):
    config_path = str(tmp_path / 'config.json')
    state_path = str(tmp_path / 'alerts_state.json')
    config_store.save_config(config_path, config_store.default_config())

    cadence = poller.run_one_cycle(config_path, state_path, _fake_get_conn)

    assert cadence == config_store.DEFAULT_POLL['slow_s']
    status = poller.status()
    assert status['ok'] is True


def test_run_one_cycle_fetches_evaluates_and_saves_state(tmp_path, monkeypatch):
    config_path = str(tmp_path / 'config.json')
    state_path = str(tmp_path / 'alerts_state.json')
    config_store.save_config(config_path, {
        'instances': [_instance()], 'poll': config_store.DEFAULT_POLL,
        'thresholds': config_store.DEFAULT_THRESHOLDS, 'notify': {'webhook_url': None},
    })

    fake_summary = [{
        'id': 'inst-1', 'name': 'inst-1', 'reachable': True,
        'pool_usage': [{'pool': 'tank', 'pct': 95.0}],
        'active_alerts': [],
    }]
    monkeypatch.setattr(poller, 'fetch_fleet', lambda instances, get_conn: fake_summary)

    poller.run_one_cycle(config_path, state_path, _fake_get_conn)

    from core import alerts
    state = alerts.load_state(state_path)
    assert any(k.endswith('|capacity|tank') for k in state['conditions'])
    assert state['conditions']['inst-1|capacity|tank']['level'] == 'crit'


def test_run_one_cycle_sends_webhook_when_notifications_fire(tmp_path, monkeypatch):
    config_path = str(tmp_path / 'config.json')
    state_path = str(tmp_path / 'alerts_state.json')
    config_store.save_config(config_path, {
        'instances': [_instance()], 'poll': config_store.DEFAULT_POLL,
        'thresholds': config_store.DEFAULT_THRESHOLDS,
        'notify': {'webhook_url': 'http://example.invalid/hook'},
    })
    fake_summary = [{
        'id': 'inst-1', 'name': 'inst-1', 'reachable': True,
        'pool_usage': [{'pool': 'tank', 'pct': 95.0}], 'active_alerts': [],
    }]
    monkeypatch.setattr(poller, 'fetch_fleet', lambda instances, get_conn: fake_summary)
    sent = {}
    monkeypatch.setattr(poller.notify, 'send_webhook',
                         lambda url, notifications: sent.setdefault('call', (url, notifications)) or (True, None))

    poller.run_one_cycle(config_path, state_path, _fake_get_conn)

    assert sent['call'][0] == 'http://example.invalid/hook'
    assert len(sent['call'][1]) == 1


def test_run_one_cycle_survives_a_crash_and_reports_not_ok(tmp_path, monkeypatch):
    config_path = str(tmp_path / 'config.json')
    state_path = str(tmp_path / 'alerts_state.json')
    config_store.save_config(config_path, {
        'instances': [_instance()], 'poll': config_store.DEFAULT_POLL,
        'thresholds': config_store.DEFAULT_THRESHOLDS, 'notify': {'webhook_url': None},
    })

    def boom(instances, get_conn):
        raise RuntimeError('simulated crash')

    monkeypatch.setattr(poller, 'fetch_fleet', boom)

    cadence = poller.run_one_cycle(config_path, state_path, _fake_get_conn)

    assert cadence == config_store.DEFAULT_POLL['slow_s']  # never propagates, still returns a cadence
    status = poller.status()
    assert status['ok'] is False
    assert 'simulated crash' in status['error']


def test_start_is_idempotent_does_not_spawn_a_second_thread(tmp_path):
    config_path = str(tmp_path / 'config.json')
    state_path = str(tmp_path / 'alerts_state.json')
    config_store.save_config(config_path, config_store.default_config())
    try:
        poller.start(config_path, state_path, _fake_get_conn)
        first_thread = poller._thread
        poller.start(config_path, state_path, _fake_get_conn)
        assert poller._thread is first_thread
    finally:
        poller.stop(timeout=2)


def test_stop_actually_joins_the_thread(tmp_path):
    config_path = str(tmp_path / 'config.json')
    state_path = str(tmp_path / 'alerts_state.json')
    config_store.save_config(config_path, config_store.default_config())
    poller.start(config_path, state_path, _fake_get_conn)
    assert poller._thread.is_alive()
    poller.stop(timeout=2)
    assert not poller._thread.is_alive()
