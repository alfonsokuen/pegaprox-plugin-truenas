# API (v0.9.0)

All routes are under `/api/plugins/truenas/api/<path>`. Every per-instance
route takes `instance_id` as a **query param** (e.g.
`GET .../pools?instance_id=datos-64`), never a URL path segment — the only
CONFIRMED-in-production plugin routing mechanism maps one fixed path string
per handler (see the module docstring in `src/routes/api.py`).

## Config & connection

| Method | Path | Auth | Body | Response |
|---|---|---|---|---|
| GET | `ui` | `storage.view` | — | The UI shell (`text/html`) |
| GET | `config` | admin | — | `{"instances": [...masked], "instances_by_client": [...], "poll": {...}}` |
| POST | `config/save` | admin | `{"instances": [...], "poll": {...}}` | `{"ok": true, "instances": N}` or `{"error": "..."}` (400) |
| POST | `instances/test` | admin | `{"id"?, "host", "port", "use_tls"?, "verify_tls"?, "api_key_ro"}` | `{"ok": bool, "error": str\|null}` |

`instances/test` connects and calls `auth.login_with_api_key`, nothing
else. It never persists to `config.json`.

## Reads (`storage.view`, query param `instance_id`)

| Path | Backing subsystem |
|---|---|
| `system` | `system.info` + alerts |
| `pools` | `pool.query` |
| `datasets` | `pool.dataset.query` |
| `snapshots` | `zfs.snapshot.query` |
| `shares` | SMB + NFS + iSCSI target/extent/targetextent (5-way, one dict keyed by kind) |
| `replication` | `replication.query` |
| `apps_vms` | `vm.query` + `app.query` (one dict keyed by kind) |
| `services` | `service.query` |
| `data_protection` | `cloudsync.query` / `rsynctask.query` / `certificate.query` — **allow-listed fields only**, never the raw record (see README "F6") |
| `telemetry` | `reporting.get_data` (cpu/memory/network, downsampled ≤120 points) |
| `fleet` (no `instance_id` — aggregates every configured instance) | fans out `system.info` + recent audit events across all instances |

## Writes

Both write routes are admin-gated and share the same `WRITE_OPS` registry
(`src/routes/api.py`) — a dry-run preview can never describe a different
call than what actually executes.

| Method | Path | Body |
|---|---|---|
| POST | `writes/dry-run` | `{"instance_id", "subsystem", "op", "payload"}` → `{method, params}`, never touches TrueNAS |
| POST | `writes/execute` | same body → executes, then verify + audit |

`(subsystem, op)` pairs currently registered:

| Subsystem | Ops |
|---|---|
| `datasets` | `create`, `update`, `delete` |
| `snapshots` | `create`, `delete` |
| `services` | `start`, `stop`, `restart` |
| `vms` | `start`, `stop`, `restart` |
| `apps` | `start`, `stop`, `redeploy` (no `restart` — doesn't exist on this TrueNAS version) |
| `smb_shares` | `create`, `update`, `delete` |
| `nfs_shares` | `create`, `update`, `delete` |

`delete` ops require a typed `confirm_name` (or `confirm_path` for NFS)
matching the resource's real name/path/id, checked inside the pure
`build_<op>_envelope(...)` builder before any envelope exists.

iSCSI has no write ops — read-only by design (see README "Known gaps").
