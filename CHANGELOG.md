# Changelog

## [0.3.0] - 2026-07-20

F2 — first real writes: datasets/zvols and snapshots create/update/delete
against `.64` (confirmed no longer production-critical), built entirely
with fakes/mocks per this phase's hard safety guard — no real key, no
real call against `.64` in this repo's own tests or code path; the
operator connects the real `svc-pegaprox-rw` account and runs the first
live write in a separate session after this passes review.

- **Write-path (brief §5) implemented literally**: for every op, a pure
  `build_<op>_envelope(...)` function (no `conn` parameter at all) returns
  `(method, params)` or raises; the real `<op>(conn, ...)` function calls
  that SAME builder before ever touching `conn`. `POST writes/dry-run`
  calls only the builder; `POST writes/execute` calls the identical
  builder first (so a bad typed confirmation 400s before any connection
  is even opened) and then the real op. This is a structural guarantee,
  not a convention: dry-run cannot describe a different JSON-RPC call than
  what execute actually runs, because it is the same function call.
- **`datasets.py`**: `create`/`update`/`delete` wrapping
  `pool.dataset.create`/`update`/`delete`. `delete` requires
  `confirm_name` to match the dataset's full path exactly (GitHub-style
  typed confirmation) — checked inside the builder, before any TrueNAS
  call is attempted.
- **`snapshots.py`**: `create`/`delete` wrapping `pool.snapshot.create`/
  `delete`, same typed-confirmation guard on delete (full `dataset@name`
  snapshot id).
- **`ConnectionManager.get_rw_connection()`**: a SEPARATE cached client
  from `get_connection()`'s read-only one. Writes authenticate with
  `api_key_rw` on this dedicated connection — the shared read connection
  (used by every F1 tab) is never touched, so it can never get silently
  upgraded to RW privilege by a write elsewhere. `close()`/`close_all()`
  now drop both.
- **`_resolve_writable_instance`**: existence + `readonly is False` +
  `api_key_rw` present, ALL checked before an envelope is even built —
  `readonly` (the F0 kill-switch) remains the final server-side authority
  regardless of what the UI shows.
- **Post-write verify (step 6) + no-auto-retry (step 8)**: every execute
  re-reads the resource after the call and reports one of `ok` /
  `pending` / `verify_failed` / `error` — never silently retried. Audit
  (`log_audit`, same `details`-string pattern as F0's `client_id`
  decision — see below) fires for every outcome, success or failure, with
  a `params_hash` (sha256, truncated) instead of the raw payload so
  dataset properties/quotas don't bloat the audit log.
- **UI**: Datasets/Snapshots tabs gained create/edit/delete actions:
  form → "Vista previa" (dry-run, shows the literal method+params) →
  "Confirmar y ejecutar", which for delete stays disabled until the typed
  confirmation field matches the resource's full name exactly — mirroring
  the server-side guard so the UI never promises a delete the API would
  refuse anyway.
- **Sync-vs-async design decision (unresolved without live access, per
  this phase's explicit instruction to design conservatively)**: whether
  `pool.dataset.create`/`update`/`delete` and `pool.snapshot.create`/
  `delete` are synchronous or job-wrapped in TrueNAS 25.10.1 was NOT
  confirmed. Every write result is checked for `isinstance(result, int)`
  (TrueNAS's convention for a job id); if verify doesn't yet show the
  expected state and the result was an int, status is reported as
  `pending` (genuinely unknown: job still running vs. actually failed),
  never asserted as a false success or failure. No job poller is built —
  out of scope per this phase — so `pending` comes with a re-check path
  (call `writes/execute` again) rather than silent uncertainty.
- 213 tests (up from 164), verified via `pytest --collect-only -q`. 94%
  combined coverage on `core/`+`routes/`+`subsystems/` (every individual
  module ≥91%). All write-path tests use fakes — zero real calls, zero
  real dataset/pool names (fixtures use `tank/test-dataset`, never
  `IDK_LOCAL` or any real `.64` identifier).

## [0.2.0] - 2026-07-20 (post-review hardening, round 3)

Two independent reviews (code-reviewer + silent-failure-hunter) converged
independently on the same concurrency bug plus a shared "all-or-nothing
fetch" pattern across F1's multi-call subsystems.

- **`_do_login`'s `_authenticated = True` assignment race**: it used to be
  set unconditionally right after the login RPC returned. If the socket
  dropped in the window between the response arriving and that assignment,
  the reader thread had already set `_authenticated = False` under
  `_connect_lock` — the unconditional write silently overwrote that with a
  lie (`_connected: False`, `is_authenticated: True`), so the next request
  would skip `login()` entirely against a fresh, session-less socket. Now
  assigned atomically under `_connect_lock`, gated on the socket still
  being the one that answered: raises `TrueNASConnectionError` instead of
  lying if it dropped mid-login. Same "state that lies" bug class this
  file already fixed three times for `_connected`/`_closed` in F0 — forced
  the exact interleave in a regression test via a `client.call` wrapper.
- **`core.subsystem.safe_call`**: new shared helper — call a sub-RPC,
  degrade to a default and log a warning on `TrueNASError` instead of
  letting one sub-call sink an entire multi-call response. Applied to:
  - `pools` route fetch: `disk.query`/`disk.temperature_agg` now degrade
    independently of `pool.query` — the real risk scenario (brief §4.3/§9)
    is a disk failing SMART in a pool that's still `ONLINE`, exactly where
    a hung/erroring temperature query used to also take down pool
    status/health.
  - `system` route fetch: `system.info`/`alert.list`/`update.status` each
    degrade independently — `update.status` (the least critical, and per
    its own docstring the one whose "no update" shape was never captured
    live) used to also hide alerts/health if it failed.
  - `shares.list()`: all 5 collections (SMB/NFS/3× iSCSI) degrade
    independently — a failing `iscsi.*` query used to also hide a working
    SMB/NFS listing.
  - `apps_vms.list()`: `app.query`/`vm.query` degrade independently —
    `vm.query` (the namespace already flagged as unstable across TrueNAS
    versions) failing used to also hide `apps`, which responded fine.
  - Every degraded fetch now carries a `<key>_error` field (`None` on
    success) alongside the data, surfaced in the UI as an inline hint
    rather than silently vanishing.
- **`_subsystem_route`'s 502 path now logs a warning** — the expected
  failure case (appliance down, timeout, revoked key) used to leave zero
  server-side trace; only whoever had the browser tab open ever saw it.
- **UI: both new F1 fetch chains (`Promise.all` for Overview,
  `fetchSubsystem` for every other tab) now have `.catch()`** — a rejected
  fetch (network down, PegaProx session expired returning HTML instead of
  JSON) used to leave the tab stuck on "Cargando…" forever with an
  unhandled rejection muted in the console. Deliberately does not mark the
  tab as loaded on error, so the next click/instance-change retries.
- **UI: Overview/Pools no longer cache** (every other F1 tab still does) —
  they're the only ones showing live resilver/scrub progress, so caching
  them could leave a stale % on screen for hours if the tab stays open.
  Both now show an "actualizado HH:MM:SS" timestamp.
- **`datasets.quota()`**: fixed a docstring that referenced a
  `list_with_quotas` sweep that doesn't exist anywhere in the repo (nothing
  calls `quota()` yet outside its own tests) — clarified it's a
  standalone, not-yet-wired helper for a future per-dataset quota display,
  and added a `log.warning` inside its except branch (dataset id + cause)
  for when it IS wired in F1.5/F2.
- **`needs_auth` is now actually consumed**: `_do_login` sets it on ANY
  rejected login (not only the reconnect-triggered relogin path it was
  previously limited to), and `_get_authenticated_connection` fails fast
  on it instead of retrying the identical doomed `login()` call against a
  key already proven bad — stops hammering the appliance with repeated
  failed-auth attempts on every poll once a key is known revoked.
- 164 tests (up from 149), verified via `pytest --collect-only -q`. 94%
  combined coverage on `core/`+`routes/`+`subsystems/` (every individual
  module ≥90%).

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
