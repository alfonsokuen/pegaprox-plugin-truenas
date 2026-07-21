# -*- coding: utf-8 -*-
"""Validate the plugin entry point wires every documented F0 route."""

from core import poller


def test_register_wires_all_routes(plugin, monkeypatch, tmp_path):
    captured = {}

    def fake_register(plugin_id, path, handler):
        captured.setdefault(plugin_id, {})[path] = handler

    monkeypatch.setattr(plugin, 'register_plugin_route', fake_register)
    # register() also starts the F4a background poller against
    # PLUGIN_DIR/config.json for real. Found live (2026-07-21): a
    # developer's own local checkout can have a REAL config.json sitting
    # in the repo root (gitignored, used for manual live-testing against
    # the real TrueNAS instance) — this test used to assume "no
    # config.json here" and, when that assumption was wrong, the poller
    # it started made REAL network calls to REAL production from a plain
    # `pytest` run. Repointing PLUGIN_DIR at an empty tmp_path makes this
    # test hermetic regardless of what happens to be sitting in the repo
    # root on whoever's machine runs it.
    monkeypatch.setattr(plugin, 'PLUGIN_DIR', str(tmp_path))
    try:
        plugin.register(app=None)

        routes = captured.get('truenas', {})
        expected = {
            'ui', 'config', 'config/save', 'instances/test',
            'system', 'pools', 'datasets', 'snapshots', 'shares',
            'replication', 'apps_vms', 'services', 'data_protection', 'telemetry', 'fleet',
            'poller/status', 'writes/dry-run', 'writes/execute',
        }
        assert set(routes) == expected
        assert all(callable(h) for h in routes.values())
    finally:
        poller.stop(timeout=2)


def test_plugin_id_matches_manifest(plugin):
    import json
    import os
    manifest_path = os.path.join(plugin.PLUGIN_DIR, 'manifest.json')
    with open(manifest_path) as f:
        manifest = json.load(f)
    assert plugin.PLUGIN_ID == 'truenas'
    assert manifest['version'] == '0.14.0'
    assert manifest['has_frontend'] is True
    assert manifest['frontend_route'] == 'ui'
