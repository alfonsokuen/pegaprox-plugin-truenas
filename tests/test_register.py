# -*- coding: utf-8 -*-
"""Validate the plugin entry point wires every documented F0 route."""


def test_register_wires_all_routes(plugin, monkeypatch):
    captured = {}

    def fake_register(plugin_id, path, handler):
        captured.setdefault(plugin_id, {})[path] = handler

    monkeypatch.setattr(plugin, 'register_plugin_route', fake_register)
    plugin.register(app=None)

    routes = captured.get('truenas', {})
    expected = {
        'ui', 'config', 'config/save', 'instances/test',
        'system', 'pools', 'datasets', 'snapshots', 'shares',
        'replication', 'apps_vms', 'services', 'data_protection', 'telemetry', 'fleet',
        'writes/dry-run', 'writes/execute',
    }
    assert set(routes) == expected
    assert all(callable(h) for h in routes.values())


def test_plugin_id_matches_manifest(plugin):
    import json
    import os
    manifest_path = os.path.join(plugin.PLUGIN_DIR, 'manifest.json')
    with open(manifest_path) as f:
        manifest = json.load(f)
    assert plugin.PLUGIN_ID == 'truenas'
    assert manifest['version'] == '0.13.0'
    assert manifest['has_frontend'] is True
    assert manifest['frontend_route'] == 'ui'
