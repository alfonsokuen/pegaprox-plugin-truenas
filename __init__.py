# -*- coding: utf-8 -*-
"""
TrueNAS — PegaProx Plugin
Codename: truenas

F0 (v0.1.0): installable skeleton. Ships the persistent, reconnecting
WebSocket JSON-RPC 2.0 client (``src/core/ws_client.py``), a lazy-connect
per-instance connection manager (``src/core/conn_manager.py``), multi-client
multi-instance config with masked API keys, and a UI shell with empty tabs.
No subsystem (pools/datasets/snapshots/shares/replication/apps_vms) is
implemented yet — that is F1+. See PEGAPROX_PLUGIN_TRUENAS_BRIEF.md.

Bootstrap: PegaProx does NOT add a plugin's own directory to ``sys.path`` —
replicate that injection here (same pattern as pegaprox-plugin-wake-on-lan)
so ``src/core/*`` and ``src/routes/*`` import as top-level ``core``/``routes``
packages regardless of where PegaProx happens to import this file from.

Author: IDKMANAGER
License: MIT
"""

import logging
import os
import sys

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(PLUGIN_DIR, 'src')
for _p in (PLUGIN_DIR, _SRC_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pegaprox.api.plugins import register_plugin_route  # noqa: E402

from routes import api as routes_api  # noqa: E402

PLUGIN_ID = routes_api.PLUGIN_ID
log = logging.getLogger(f'plugin.{PLUGIN_ID}')


def register(app=None):
    routes_api.init(PLUGIN_DIR)
    for path, handler in routes_api.ROUTES.items():
        register_plugin_route(PLUGIN_ID, path, handler)
    log.info(f'[{PLUGIN_ID}] Registered {len(routes_api.ROUTES)} routes')
    # TEMPORARILY DISABLED 2026-07-21: live on CT119, the poller's periodic
    # _get_authenticated_connection() calls correlate with real TrueNAS
    # sockets dropping every ~60s (poll.slow_s) and a subsequent relogin
    # being rejected ("unexpected authenticator run state") — both
    # instances ended up marked unreachable, which would also break
    # browser-driven routes sharing the same cached connection. Disabled
    # here while the root cause (likely a race between this and
    # ws_client's own _background_reconnect) is investigated properly —
    # re-enable only after a real fix, not a guess.
    # routes_api.start_poller()
