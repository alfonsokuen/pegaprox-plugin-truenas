# TrueNAS — PegaProx Plugin

Monitors and controls one or more TrueNAS SCALE instances from the PegaProx
panel (TrueCommand-style), over the JSON-RPC 2.0 WebSocket API — REST v2.0
is deprecated in 25.10 and removed in TrueNAS 26, so this plugin never uses
it. See `PEGAPROX_PLUGIN_TRUENAS_BRIEF.md` (in the workspace, not this repo)
for the full architecture, phase plan and known gotchas.

**This is F1 (v0.2.0): full read-only monitoring**, on top of the F0
transport layer/config/UI shell — verified live against the real `.64`
instance (login + `system.info` + `alert.list` + `pool.query`). Every tab
(Overview/Pools & Discos/Datasets/Snapshots/Shares/Replicación/Apps-VMs/
Settings) now shows real data. No writes anywhere yet — create/update/
delete on any subsystem is F2+.

## What's in F1 (on top of F0)

- `src/core/subsystem.py` — the `Subsystem` contract (`list`/`read`/
  `health`, `write()` read-only by default) every module in
  `src/subsystems/` implements.
- `src/subsystems/{system,pools,datasets,snapshots,shares,replication,
  apps_vms}.py` — one module per TrueNAS concept, each wrapping the exact
  JSON-RPC methods from brief §4.2. See CHANGELOG.md `[0.2.0]` for the
  full per-module method list and the pools temperature-exclusion safety
  correction (brief §4.3/§9).
- 7 new read routes in `src/routes/api.py`, `instance_id` as a query param
  (see the module docstring for why — the confirmed plugin routing
  mechanism doesn't support URL path parameters).
- `TrueNASWSClient.is_authenticated` — tracks the CURRENT socket's live
  session, distinct from "an api_key is remembered for relogin".
- `src/ui/plugin.html` — every tab fetches and renders real data now.

## What's in F0

- `src/core/ws_client.py` — a generic, reusable JSON-RPC 2.0 client over a
  persistent WebSocket: request/response framing, concurrent `id` handling,
  typed errors, lazy-connect, and automatic reconnection (exponential
  backoff + jitter) that re-logs-in and re-subscribes after a drop.
- `src/core/conn_manager.py` — one client per configured instance,
  lazy-connect, multi-instance from day one.
- `config.example.json` / `config.json` (chmod 600, never committed) —
  multi-tenant instance list (`client_id` + host/port/TLS/API keys/readonly)
  and the polling budget.
- `src/routes/api.py` + `src/routes/config_store.py` — the `ui`, `config`,
  `config/save` and `instances/test` routes, with masked-key round-tripping.
- `src/ui/plugin.html` — the UI shell: instance selector grouped by client,
  placeholder tabs, and a functional Settings tab.

## Multi-tenant (brief §3.1)

This plugin will eventually manage TrueNAS instances belonging to
**different clients** in the same panel (IDKmanager, SACEI, INGESA,
GeoSpace, ...), not just the operator's own infrastructure. Every instance
in `config.json` carries a free-form `client_id` field from F0 onward — the
Settings UI and the instance selector group by it. **F0 does not yet call
PegaProx's `check_cluster_access`** for per-client scoping — that mapping
(client_id ↔ Proxmox cluster) lands in F1+, once more than one real client
is on-boarded. Until then, every write in this plugin (config, instance
test) is gated on the admin role, same as the rest of the plugin's RBAC.

## RBAC

PegaProx's `PERMISSIONS` table is fixed — a plugin cannot register new
assignable permissions. This plugin reuses:

| Action | Permission |
|---|---|
| `ui` (the tab itself) | `storage.view` |
| `system`, `pools`, `datasets`, `snapshots`, `shares`, `replication`, `apps_vms` (F1 reads) | `storage.view` |
| `config`, `config/save`, `instances/test` (touch API keys) | admin role only |

`admin` always passes any `has_permission` check automatically.

## Configuration

Each entry in `instances[]`:

| Field | Meaning |
|---|---|
| `id` | Stable identifier, unique within this plugin's config |
| `name` | Display name |
| `client_id` | Free-form tenant namespace (`idkmanager`, `sacei`, ...) |
| `host` / `port` | TrueNAS UI host and port |
| `use_tls` | **Must be `true`** whenever an API key is set — TrueNAS auto-revokes a key used over plain HTTP. `config/save` rejects the combination. |
| `verify_tls` | Whether to verify the appliance's TLS certificate (usually self-signed → `false`) |
| `api_key_ro` / `api_key_rw` | Service-account keys (`svc-pegaprox-ro`/`svc-pegaprox-rw`); masked as `***` on every `GET config`, round-tripped on save |
| `readonly` | Server-evaluated kill-switch — stays effective even if `api_key_rw` is set |

`poll.{fast_s,slow_s,cold_s}` — the polling budget from the brief §4.3;
unused in F0 (no subsystem polls anything yet), validated and persisted for
F1 to consume.

## `instances/test`

Connects the WebSocket and calls `auth.login_with_api_key` — nothing else
(no subsystem call). Used from the Settings tab's "Probar conexión" button,
works against either a saved instance (`id`) or an unsaved draft from the
form, and never persists anything to `config.json`. (F0 called this "the
only real TrueNAS interaction allowed" — F1 adds seven read-only ones on
top, see above.)

## Design decisions taken where the brief was ambiguous

- **Reconnect trigger, not a background poller.** The client's automatic
  reconnection only runs after the WebSocket reader thread observes an
  *unexpected* disconnect (a `recv()` failure) — there is no separate
  keepalive/health-check thread pinging the socket. This satisfies "backoff
  + jitter reconnection" without adding a second timer to reason about in
  F0; F1's job-tracking loop can add liveness probing if TrueNAS's own
  socket timeout proves too silent in practice.
- **Relogin/resubscribe only on recovery, not on the very first connect.**
  An earlier draft called `_relogin_and_resubscribe()` unconditionally after
  every successful `connect()`. That recursed: `subscribe()` registers its
  callback *before* issuing the `core.subscribe` call, so the first-ever
  connect (triggered lazily by that same `subscribe()`) would re-enter
  `call()` for the same subscription while the outer call was still
  in-flight. Fixed by only running relogin/resubscribe from the
  `_background_reconnect` path (a genuine post-drop recovery), never from
  plain lazy-connect.
- **`log_audit(..., cluster=client_id)` not used as written in brief §3.1.**
  Without a live PegaProx host to confirm `log_audit`'s real keyword
  arguments, passing an unverified `cluster=` kwarg risked a runtime
  `TypeError` in production. `client_id` is instead folded into the
  `details` string of every audit call — same information, no dependency on
  an unconfirmed signature. Revisit once `pegaprox/utils/audit.py` is
  readable from this workspace.
- **`instances/test` accepts drafts, not just saved instances**, so the
  Settings "Probar conexión" button works before hitting Save (per the UI
  flow described in brief §6/§7) — it resolves a masked `api_key_ro` back to
  the stored value only when an `id` matching a saved instance is supplied.

## Design decisions taken in F1 where the brief was ambiguous

- **`instance_id` as a query param, not a URL path segment.** The brief's
  `/<instance_id>/<subsystem>` phrasing reads like a URL template, but the
  only routing mechanism confirmed in production
  (`pegaprox.api.plugins.register_plugin_route`, verified against
  `pegaprox-plugin-wake-on-lan`) maps one FIXED path string per handler —
  wake-on-lan's own dynamic routes already use query params for exactly
  this reason. Followed the proven pattern rather than assume PegaProx's
  catch-all supports path parameters it hasn't been observed to support.
- **`shares.list()`/`apps_vms.list()` return a dict, not a flat list.**
  Both wrap multiple distinct TrueNAS collections (SMB/NFS/3× iSCSI;
  apps/VMs) that the UI's own tab layout (brief §6) treats as separate
  groups — flattening them would just force the caller to re-split what
  was artificially joined.
- **No `virt.instance.*` shim for VMs.** The brief flags 25.04→25.10 moved
  VMs between Incus and libvirt namespaces. `vm.query` responds (with
  `[]`) on the real `.64` (25.10.1) today, so no shim is implemented —
  adding one now would be speculative code for a namespace not in use on
  the only instance this plugin talks to. Add it if/when a future instance
  proves `vm.query` errors and `virt.instance.query` answers instead.

## Pendiente de F1-deploy / F2+ (explicitly out of scope here)

- **`websocket-client` vendoring.** CT119 has no external DNS/internet
  access. `requirements.txt` declares the dependency, but making it
  available offline (vendored into the plugin's cache dir, or pre-installed
  from a LAN-reachable mirror) is deploy work, not build work.
- No writers on any subsystem (create/update/delete) — F2+ per the brief's
  phase table, behind the dry-run/confirm/audit write-path (brief §5).
- No `check_cluster_access` per-client RBAC — admin-only gate until a second
  real client (SACEI/INGESA/GeoSpace) is on-boarded.
- No installation on CT119, no connection to any instance besides `.64` —
  both require explicit operator confirmation in a separate session
  (brief §0.5).

## Development

```
pip install -r requirements-dev.txt
pytest -q
```

`tests/conftest.py` stubs `flask` and `pegaprox.*` at the module level so
`__init__.py` imports standalone in CI without a live PegaProx host. `core/`
tests (`tests/unit/`) never touch a real socket — a `FakeTransport` drives
`send`/`recv` through an in-memory queue.

## Deploy

`install.sh` copies `__init__.py`, `manifest.json` and `src/` into
`/opt/PegaProx/plugins/truenas`, seeds an empty `config.json` (instances are
added afterwards from the Settings tab, never shipped in the repo), and
installs a systemd timer (`truenas-maintenance.sh`) that restores the plugin
from a cache outside `/opt/PegaProx` if a PegaProx upgrade ever wipes it.

`uninstall.sh` removes the plugin, its guard timer, and its config
(including any saved API keys).

**Deployment is NOT performed by this repository's automation.** Running
`install.sh` against production (CT119/pve1) requires explicit operator
confirmation in a separate session — see brief §0.5.
