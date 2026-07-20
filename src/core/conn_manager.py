# -*- coding: utf-8 -*-
"""One WebSocket connection per configured TrueNAS instance, lazy-connect,
multi-instance from day one (per brief §2/§3 — TrueCommand-style).

Mirrors the "connection manager" pattern used by other PegaProx plugins
(e.g. the Proxmox-power plugin's node manager): a thin registry keyed by
instance id, handing out (and lazily creating) one ``TrueNASWSClient`` per
instance, with ``is_connected`` / ``connection_error`` accessors so routes
can report instance health without forcing a connect.
"""

import logging
import threading

from .errors import TrueNASError
from .ws_client import TrueNASWSClient

log = logging.getLogger('plugin.truenas.conn_manager')


class ConnectionManager:
    def __init__(self, client_factory=None):
        self._client_factory = client_factory or TrueNASWSClient
        self._clients = {}          # (instance_id, 'ro'|'rw') -> TrueNASWSClient
        self._lock = threading.Lock()

    def get_connection(self, instance_cfg):
        """Return the (lazily created) READ-ONLY client for ``instance_cfg``.
        Does NOT connect — connection happens lazily on the client's first
        ``call()``. Logged in with ``api_key_ro`` only (see routes/api.py's
        ``_get_authenticated_connection``) — never call ``.login()`` on this
        client with the RW key, or every subsequent "read-only" request
        would silently reuse an RW-privileged session. Use
        ``get_rw_connection()`` for writes; it is a SEPARATE cached client
        specifically so a write never upgrades the shared read connection's
        privilege level."""
        return self._get_or_create((instance_cfg['id'], 'ro'), instance_cfg)

    def get_rw_connection(self, instance_cfg):
        """Return the (lazily created) client dedicated to WRITE operations
        for ``instance_cfg`` — cached separately from ``get_connection()``'s
        read-only client (brief §3: minimum privilege in runtime). Logging
        in here with ``api_key_rw`` must never touch the RO client's cache
        entry, and vice versa."""
        return self._get_or_create((instance_cfg['id'], 'rw'), instance_cfg)

    def _get_or_create(self, cache_key, instance_cfg):
        """``cache_key`` is a ``(instance_id, 'ro'|'rw')`` tuple, never a
        concatenated string — an instance legitimately named e.g. ``'foo::rw'``
        would otherwise collide with the RW-cached client of an instance
        named ``'foo'``, cross-wiring privilege/host between two distinct
        instances. A tuple key makes that collision structurally
        impossible regardless of what characters an operator puts in an
        instance id."""
        with self._lock:
            client = self._clients.get(cache_key)
            if client is None:
                client = self._client_factory(
                    host=instance_cfg['host'],
                    port=instance_cfg.get('port', 443),
                    use_tls=instance_cfg.get('use_tls', True),
                    verify_tls=instance_cfg.get('verify_tls', False),
                    tls_server_name=instance_cfg.get('tls_server_name'),
                )
                self._clients[cache_key] = client
            return client

    def is_connected(self, instance_id):
        client = self._clients.get((instance_id, 'ro'))
        return bool(client and client.is_connected)

    def connection_error(self, instance_id):
        client = self._clients.get((instance_id, 'ro'))
        return client.last_error if client else None

    def test_connection(self, instance_cfg, api_key):
        """Attempt connect + login_with_api_key against ``instance_cfg`` and
        report ok/error — the ONLY real interaction with a TrueNAS instance
        allowed in F0 (routes/api.py's ``instances/test``). Makes no other
        JSON-RPC call. Never raises: always returns a result dict.

        Builds a throwaway client straight from ``instance_cfg`` via the
        factory — deliberately NOT ``get_connection()``/the registry cache.
        Reusing a cached-by-id client here would mean: instance already
        connected to host A, operator edits the Settings form to host B
        (same id) and hits "Probar conexión" -> the cached client for that
        id is already connected to A, so ``connect()`` is a no-op and the
        login round-trips against A while the route reports success for a
        B that was never actually contacted. Always closed in ``finally``
        so a test connection never leaks a live socket into the process.
        """
        client = self._client_factory(
            host=instance_cfg['host'],
            port=instance_cfg.get('port', 443),
            use_tls=instance_cfg.get('use_tls', True),
            verify_tls=instance_cfg.get('verify_tls', False),
            tls_server_name=instance_cfg.get('tls_server_name'),
        )
        try:
            client.connect()
            client.login(api_key)
        except TrueNASError as e:
            return {'ok': False, 'error': str(e)}
        except Exception as e:  # defensive: never let a transport bug 500 the route
            log.error(f"[truenas] unexpected error testing instance "
                      f"'{instance_cfg.get('id')}': {e}", exc_info=True)
            return {'ok': False, 'error': f'unexpected error: {e}'}
        else:
            return {'ok': True, 'error': None}
        finally:
            try:
                client.close()
            except Exception:
                pass

    def close(self, instance_id):
        """Closes BOTH the RO and RW clients for ``instance_id`` — a
        config change invalidating one key almost always invalidates both
        (host/port/TLS changed), and leaving a stale RW client connected
        while the RO one gets dropped would be a confusing half-reset."""
        with self._lock:
            ro_client = self._clients.pop((instance_id, 'ro'), None)
            rw_client = self._clients.pop((instance_id, 'rw'), None)
        if ro_client:
            ro_client.close()
        if rw_client:
            rw_client.close()

    def close_all(self):
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()
