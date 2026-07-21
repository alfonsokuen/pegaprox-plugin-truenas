# -*- coding: utf-8 -*-
"""core/alerts.py: edge-triggered notifications, hysteresis, cooldown,
dedup, global rate cap. The hysteresis/flapping test is the mandatory
red-test this detector needs (testing.md) — proving the anti-flood
mechanism actually suppresses a real flapping series, not just that the
code exists."""

from core import alerts

THRESHOLDS = {'warn_pct': 80, 'crit_pct': 90}


def _reading(iid='inst-1', reachable=True, pools=None, ta_alerts=None, **overrides):
    reading = {
        'instance_id': iid, 'instance_name': iid, 'reachable': reachable,
        'pools': pools or [], 'ta_alerts': ta_alerts or [],
        'warn_pct': None, 'crit_pct': None,
    }
    reading.update(overrides)
    return reading


def test_first_observation_at_warn_level_notifies_immediately():
    """A brand-new condition already in a bad state the first time the
    poller ever sees it (e.g. right after install on an already-busy NAS)
    must notify — not silently start tracking with no alert."""
    state = alerts.default_state()
    readings = [_reading(pools=[{'name': 'tank', 'used_pct': 85}])]
    new_state, notifications = alerts.evaluate(state, 1000, readings, THRESHOLDS)
    assert len(notifications) == 1
    assert notifications[0]['level'] == 'warn'
    assert notifications[0]['transition'] == 'ok->warn'


def test_no_notification_when_nothing_crosses_a_threshold():
    state = alerts.default_state()
    readings = [_reading(pools=[{'name': 'tank', 'used_pct': 40}])]
    _, notifications = alerts.evaluate(state, 1000, readings, THRESHOLDS)
    assert notifications == []


def test_sustained_same_level_does_not_refire_within_cooldown():
    state = alerts.default_state()
    readings = [_reading(pools=[{'name': 'tank', 'used_pct': 85}])]
    state, first = alerts.evaluate(state, 1000, readings, THRESHOLDS)
    assert len(first) == 1
    state, second = alerts.evaluate(state, 1060, readings, THRESHOLDS)  # +60s, same level
    assert second == []


def test_sustained_same_level_refires_after_cooldown_elapses():
    state = alerts.default_state()
    readings = [_reading(pools=[{'name': 'tank', 'used_pct': 85}])]
    state, first = alerts.evaluate(state, 1000, readings, THRESHOLDS)
    assert len(first) == 1
    later = 1000 + alerts.COOLDOWN_S + 1
    state, second = alerts.evaluate(state, later, readings, THRESHOLDS)
    assert len(second) == 1
    assert second[0]['transition'] == 'warn->warn'


def test_hysteresis_suppresses_flapping_right_at_the_threshold():
    """THE mandatory red test: a value oscillating around warn_pct=80
    across 6 consecutive polls must produce exactly 2 notifications (the
    initial ok->warn rise, and the final warn->ok recovery once it drops
    HYSTERESIS_PCT below 80) — never one per oscillation."""
    state = alerts.default_state()
    series = [75, 82, 78, 81, 79, 83, 70]  # last value actually clears (70 < 80-5)
    now = 1000
    all_notifications = []
    for pct in series:
        readings = [_reading(pools=[{'name': 'tank', 'used_pct': pct}])]
        state, notifications = alerts.evaluate(state, now, readings, THRESHOLDS)
        all_notifications.extend(notifications)
        now += 60  # one poll cycle apart — well within COOLDOWN_S

    assert len(all_notifications) == 2, (
        f'expected exactly 2 notifications (rise + recovery), got '
        f'{len(all_notifications)}: {all_notifications}'
    )
    assert all_notifications[0]['transition'] == 'ok->warn'
    assert all_notifications[1]['transition'] == 'warn->ok'


def test_hysteresis_does_not_clear_at_exactly_the_warn_boundary():
    """Dropping to exactly warn_pct (not below warn_pct - HYSTERESIS_PCT)
    must NOT clear the condition — this is the specific boundary the
    flapping test above exercises across a whole series; this test pins
    the single-step behavior directly."""
    state = alerts.default_state()
    state, _ = alerts.evaluate(
        state, 1000, [_reading(pools=[{'name': 'tank', 'used_pct': 85}])], THRESHOLDS)
    _, notifications = alerts.evaluate(
        state, 1060, [_reading(pools=[{'name': 'tank', 'used_pct': 80}])], THRESHOLDS)
    assert notifications == []


def test_crit_level_escalation_notifies_even_within_cooldown():
    state = alerts.default_state()
    state, first = alerts.evaluate(
        state, 1000, [_reading(pools=[{'name': 'tank', 'used_pct': 85}])], THRESHOLDS)
    assert first[0]['level'] == 'warn'
    _, second = alerts.evaluate(
        state, 1010, [_reading(pools=[{'name': 'tank', 'used_pct': 95}])], THRESHOLDS)
    assert len(second) == 1
    assert second[0]['level'] == 'crit'
    assert second[0]['transition'] == 'warn->crit'


def test_per_instance_threshold_override_takes_precedence_over_global():
    """warn_pct=60 override means 70% (below the GLOBAL 80%) still warns."""
    state = alerts.default_state()
    readings = [_reading(pools=[{'name': 'tank', 'used_pct': 70}], warn_pct=60)]
    _, notifications = alerts.evaluate(state, 1000, readings, THRESHOLDS)
    assert len(notifications) == 1
    assert notifications[0]['level'] == 'warn'


def test_per_instance_override_only_one_side_falls_back_for_the_other():
    """Overriding only crit_pct=95 still uses the GLOBAL warn_pct=80 for
    the warn boundary — proves the fallback is per-field, not all-or-nothing."""
    state = alerts.default_state()
    readings = [_reading(pools=[{'name': 'tank', 'used_pct': 85}], crit_pct=95)]
    _, notifications = alerts.evaluate(state, 1000, readings, THRESHOLDS)
    assert len(notifications) == 1
    assert notifications[0]['level'] == 'warn'  # 85 < 95 (overridden crit), so not crit


def test_reachability_down_then_recovery_notifies_both_edges():
    state = alerts.default_state()
    state, down = alerts.evaluate(state, 1000, [_reading(reachable=False)], THRESHOLDS)
    assert len(down) == 1 and down[0]['metric'] == 'reachability'
    _, recovered = alerts.evaluate(state, 1060, [_reading(reachable=True)], THRESHOLDS)
    assert len(recovered) == 1
    assert recovered[0]['transition'] == 'down->ok'


def test_ta_alert_relay_dedups_by_id_and_notifies_once():
    state = alerts.default_state()
    alert = {'id': 'alert-uuid-1', 'level': 'WARNING', 'formatted': 'Pool vmstore is at 91%'}
    state, first = alerts.evaluate(state, 1000, [_reading(ta_alerts=[alert])], THRESHOLDS)
    assert len(first) == 1
    # Same alert still present next poll -> no re-notification (no cooldown
    # elapsed, level unchanged: 'active' stays 'active').
    _, second = alerts.evaluate(state, 1060, [_reading(ta_alerts=[alert])], THRESHOLDS)
    assert second == []


def test_ta_alert_no_longer_present_notifies_cleared_and_drops_from_state():
    state = alerts.default_state()
    alert = {'id': 'alert-uuid-1', 'level': 'WARNING', 'formatted': 'Pool vmstore is at 91%'}
    state, _ = alerts.evaluate(state, 1000, [_reading(ta_alerts=[alert])], THRESHOLDS)
    assert any(k.endswith('alert-uuid-1') for k in state['conditions'])

    new_state, notifications = alerts.evaluate(state, 1060, [_reading(ta_alerts=[])], THRESHOLDS)
    assert len(notifications) == 1
    assert notifications[0]['transition'] == 'active->cleared'
    assert not any(k.endswith('alert-uuid-1') for k in new_state['conditions'])


def test_global_rate_cap_collapses_excess_into_one_suppressed_summary():
    state = alerts.default_state()
    # MAX_PER_HOUR + 3 distinct instances all newly crossing into warn in
    # the SAME cycle -> more raw notifications than the cap allows.
    readings = [
        _reading(iid=f'inst-{i}', pools=[{'name': 'tank', 'used_pct': 85}])
        for i in range(alerts.MAX_PER_HOUR + 3)
    ]
    _, notifications = alerts.evaluate(state, 1000, readings, THRESHOLDS)
    assert len(notifications) == alerts.MAX_PER_HOUR + 1  # +1 for the summary
    summary = notifications[-1]
    assert summary['metric'] == 'rate_limit'
    assert '3 alert(s) suppressed' in summary['message']


def test_rate_limit_window_resets_after_an_hour():
    state = alerts.default_state()
    state['rate_limit'] = {'window_start': 1000, 'count': alerts.MAX_PER_HOUR}
    readings = [_reading(pools=[{'name': 'tank', 'used_pct': 85}])]
    _, notifications = alerts.evaluate(
        state, 1000 + alerts.RATE_WINDOW_S + 1, readings, THRESHOLDS)
    assert len(notifications) == 1  # not suppressed — new window


def test_load_state_missing_file_returns_defaults(tmp_path):
    state = alerts.load_state(str(tmp_path / 'nope.json'))
    assert state == alerts.default_state()


def test_load_state_corrupt_file_logs_and_falls_back(tmp_path, caplog):
    import logging
    path = tmp_path / 'alerts_state.json'
    path.write_text('{ not valid json')
    with caplog.at_level(logging.ERROR, logger='plugin.truenas.alerts'):
        state = alerts.load_state(str(path))
    assert state == alerts.default_state()
    assert any('corrupt' in r.message for r in caplog.records)


def test_save_then_load_state_round_trips(tmp_path):
    path = str(tmp_path / 'alerts_state.json')
    state = alerts.default_state()
    state, _ = alerts.evaluate(
        state, 1000, [_reading(pools=[{'name': 'tank', 'used_pct': 85}])], THRESHOLDS)
    alerts.save_state(path, state)
    reloaded = alerts.load_state(path)
    assert reloaded['conditions'] == state['conditions']
