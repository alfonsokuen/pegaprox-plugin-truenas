# Changelog

## [0.2.0] - 2026-07-20

F1 — full read-only monitoring, on top of the WS client/conn_manager
verified live against `.64` in F0.

- **`Subsystem` contract** (`src/core/subsystem.py`): `list`/`read`/`health`
  per TrueNAS concept, `write()` raising `ReadOnlySubsystem` by default
  (every F1 subsystem is read-only; F2+ overrides `write()` behind the
  dry-run/confirm/audit pattern, brief §5). `HealthReport` dataclass with a
  `to_dict()` for JSON responses.
- **Seven subsystem modules** (`src/subsystems/`), each wrapping the
  TrueNAS JSON-RPC methods from brief §4.2:
  - `system.py` — `system.info`, `alert.list`, `update.status` (never
    `update.check_available`, removed in 25.x). Health = no active
    (non-dismissed) alert at ERROR or above.
  - `pools.py` — `pool.query`, `disk.query`, `disk.temperature_agg`.
    Carries the brief's safety correction (§4.3/§9): `pool.query`/its
    `scan` field reads pure ZFS kernel state and is safe to poll on any
    schedule, even mid-resilver; temperature polling explicitly excludes
    every disk belonging to a currently DEGRADED/FAULTED/UNAVAIL pool
    (walks `topology` recursively to resolve pool → disk device names).
  - `datasets.py` — `pool.dataset.query` + best-effort
    `pool.dataset.get_quota` (a bad dataset id degrades to `[]` for that
    dataset only, never fails the whole sweep).
  - `snapshots.py` — `pool.snapshot.query` + `pool.snapshottask.query`.
  - `shares.py` — SMB/NFS/iSCSI (5 TrueNAS collections); `list()`
    deliberately returns a dict keyed by kind, not a flattened list — the
    UI's own SMB/NFS/iSCSI tabs need them separate anyway.
  - `replication.py` — `replication.query`.
  - `apps_vms.py` — `app.query` + `vm.query`. Both confirmed live against
    the real `.64` (25.10.1) responding `[]`; no `virt.instance.*` shim
    added — would be speculative code for a namespace not in use on the
    only instance this plugin talks to today.
- **7 new read routes** (`GET .../system|pools|datasets|snapshots|shares|
  replication|apps_vms`), gated by `storage.view`. Deliberate deviation
  from the brief's illustrative `/<instance_id>/<subsystem>` URL template:
  `instance_id` travels as a query param, matching the only CONFIRMED
  plugin routing mechanism (`register_plugin_route` maps one fixed path
  string per handler — no path parameters, same pattern already used by
  wake-on-lan's `job`/`status` routes). Shared error handling
  (`_resolve_instance` / `_get_authenticated_connection` /
  `_subsystem_route`) resolves the instance from config, lazily
  connects+logs in with `api_key_ro` (never RW, even if configured), and
  turns any `TrueNASError` into a clear-context JSON response — never a
  bare, unexplained 500.
- **`TrueNASWSClient.is_authenticated`**: distinct from "an api_key is
  remembered" — tracks whether the CURRENT socket has a live, successful
  session. Goes `False` on `close()`, a torn-down failed relogin, or an
  unexpected disconnect, even before the background worker gets a chance
  to relogin. The subsystem routes gate their first call per request on
  this so a persistent, cached-per-instance connection only logs in once,
  not on every poll.
- **UI**: Overview/Pools & Discos/Datasets/Snapshots/Shares/Replicación/
  Apps-VMs now fetch and render real data (bento health cards + live
  resilver/scrub progress bars on Overview, per-pool status + temperature
  table on Pools, plain tables elsewhere) instead of placeholder text.
  Settings is unchanged. No design-system pass this round — functionality
  over polish per this phase's explicit scope.
- 149 tests (unit + route-level), verified via `pytest --collect-only -q`.
  93%+ combined coverage on `core/` + `routes/` + `subsystems/` (every
  individual module ≥90%).

Still F1 scope only: no writes anywhere (create/update/delete is F2+), no
connection to any instance besides `.64`, no deploy/push.

## [0.1.0] - 2026-07-20

Initial release — F0 (installable skeleton).

### Post-release correction (same day, before any deploy)

Live verification against the real `.64` instance (SSH + `midclt call` +, in
the end, a real WebSocket session) shook out three things, in this order:

1. **TLS**: `.64:81` is HTTP-only (`openssl s_client` fails outright — no TLS
   at all). `.64` already serves valid HTTPS (real Let's Encrypt cert, not
   self-signed) on port `444` (`system.general.config.ui_httpsport`) — no
   TrueNAS configuration change was needed, just correcting the plugin's
   assumption. `config.example.json` updated to `port: 444, verify_tls: true`.
2. **WebSocket path, corrected TWICE (net: back to the original)**: first
   read `.64`'s `nginx.conf` and concluded `/websocket` (a dedicated,
   active `proxy_pass` location) must be the real JSON-RPC endpoint over
   `/api/current` (a generic `/api` prefix match) — wrong conclusion.
   Actually connecting to `/websocket` with a JSON-RPC envelope crashed
   `middlewared` server-side (`websocket_app.on_message(): KeyError: 'msg'`)
   — `/websocket` speaks the OLD legacy DDP protocol, not JSON-RPC.
   Reading `middlewared/main.py` directly settled it: `/api/{version}`
   (including the key `"current"`) is routed to `RpcWebSocketHandler` — the
   real JSON-RPC 2.0 handler. `/api/current` was right from the start;
   `ws_client.url()` reverted.
3. **TLS/SNI mismatch discovered by the above test**: `.64`'s cert is
   issued for `CN=nube.idkmanager.com`, not for its LAN IP — connecting by
   IP with `verify_tls: true` failed with "IP address mismatch" even though
   the cert itself is valid. Added `tls_server_name` (optional, per
   instance): overrides the TLS/SNI verification name independently of the
   literal dial host, so the plugin can connect by LAN IP while verifying
   against `nube.idkmanager.com`. Threaded through
   `TrueNASWSClient.__init__` → `_default_transport_factory` →
   `websocket.create_connection(..., sslopt={'server_hostname': ...})` →
   `conn_manager` → `config_store`/`config.example.json`.

**End-to-end proof, real instance, read-only, 2026-07-20**: connect + login
(`svc-pegaprox-ro`, `READONLY_ADMIN`) + `system.info` + `alert.list` (12
active) + `pool.query` all succeeded over the actual plugin code. Pool
health is meaningfully better than the ~2-month-old ops memory assumed:
`DATA10TBX4TB` and the camera-NVR pool (now named `frigate`) are both
`ONLINE`/healthy; only `Backup_Proxmox` remains `DEGRADED`.

- Generic, reusable JSON-RPC 2.0 client over a persistent WebSocket
  (`src/core/ws_client.py`) for the TrueNAS SCALE middleware
  (`wss://<host>:<port>/api/current`): request/response framing with
  concurrent `id` handling, typed timeouts/errors (`TrueNASConnectionError`,
  `TrueNASTimeoutError`, `TrueNASRPCError`, `TrueNASAuthError`), lazy-connect
  (no network I/O at import/construction time), and automatic reconnection
  with exponential backoff + jitter that re-logs-in and re-subscribes after
  an unexpected drop.
- `login(api_key)` (`auth.login_with_api_key`) — never logs the key itself,
  including on failure.
- Event subscription hook (`subscribe`/`unsubscribe`, wired to
  `core.subscribe`) prepared for F1's job tracking (`core.get_jobs`); not
  exercised by any F0 route.
- Per-instance connection manager (`src/core/conn_manager.py`), lazy-connect,
  multi-instance from day one.
- Multi-tenant config schema (`config.example.json`): every instance carries
  a free-form `client_id` (`idkmanager`, `sacei`, `ingesa`, `geospace`, ...)
  so the plugin can host TrueNAS instances belonging to different clients in
  the same PegaProx panel — the field is persisted and used to group the
  Settings UI and instance selector; the real `check_cluster_access` gate per
  client is deferred to F1+.
- Config round-trip masking (`***`) for `api_key_ro`/`api_key_rw`, atomic
  `config.json` writes (chmod 600), and a hard safety guard rejecting
  `use_tls: false` whenever an API key is configured (TrueNAS auto-revokes a
  key used over plain HTTP).
- Routes (`/api/plugins/truenas/api/*`): `ui`, `config` (GET, masked),
  `config/save` (POST), `instances/test` (POST — the only real interaction
  with a TrueNAS instance allowed in F0: connect + `auth.login_with_api_key`,
  nothing else, never persisted).
- RBAC via existing PegaProx builtin verbs (`storage.view` for the UI shell;
  admin role for config/instance-test, since they touch credentials) — the
  plugin cannot register new assignable permissions.
- UI shell (`src/ui/plugin.html`, vanilla HTML/CSS/JS, no build step, no
  CDN): instance selector grouped by client, empty placeholder tabs for
  Overview/Pools & Discos/Datasets/Snapshots/Shares/Replicación/Apps-VMs, and
  a functional Settings tab (instance CRUD + "Probar conexión"). Theme
  inherited via `?theme=cloud`.
- Install/uninstall scripts mirroring `pegaprox-plugin-wake-on-lan`'s proven
  pattern: cache outside `/opt/PegaProx` (`/usr/local/lib/truenas`) +
  `truenas-maintenance.timer` persistence guard, SQLCipher-safe enable
  fallback, systemd-user-aware chown.
- Connection-lifecycle hardening (post-review, two rounds): the reader
  thread no longer dies on a malformed frame; a failed relogin after
  reconnect tears down the half-authenticated socket instead of reporting
  a healthy connection that isn't; `close()` atomically cancels any
  in-flight/future automatic reconnect (closing a TOCTOU window between
  the "is it closed?" check and the reconnect worker acquiring the
  connection lock, where a race could otherwise resurrect the socket and
  relogin with a stale API key); a non-blocking guard prevents duplicate
  reconnect workers; the transport clears its recv timeout after connect
  so an idle-but-healthy connection doesn't churn through reconnect+relogin
  every ~10s; `conn_manager.test_connection()` always builds a throwaway
  client from the given config instead of reusing an id-cached client
  (which could report success while testing a stale host); `instances/test`
  now applies the same use_tls-with-key safety guard as the save path.
- 74 tests (unit + route-level), verified via `pytest --collect-only -q`.
  92%+ line coverage on `core/`, 88%+ on `routes/`.

No subsystem (pools/datasets/snapshots/shares/replication/apps_vms) is
implemented yet — every non-Settings tab is empty chrome. See
`PEGAPROX_PLUGIN_TRUENAS_BRIEF.md` for the F1+ roadmap.
