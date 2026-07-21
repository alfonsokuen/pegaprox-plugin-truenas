# Changelog

## [0.10.2] - 2026-07-21 (fail fast on a permanently-expired TLS cert)

Root-caused the operator's "`.64` no me carga datos" + the recurring
"Unexpected token '<' ... not valid JSON" browser error, which turned
out to be the SAME bug, not two separate ones.

- `.64`'s TLS certificate expired 2026-07-21. `use_tls`+`verify_tls` are
  both `true` for that instance, so every connection attempt fails with
  `ssl.SSLCertVerificationError`. Before this fix, `_connect_with_backoff`
  treated that exactly like a transient blip and retried the FULL
  5-attempt exponential-backoff cycle — measured live: **~14.5s** just to
  fail, on every single request to that instance.
- Confirmed live (`curl` through the real public domain, not just
  localhost) that this plugin's own Flask route always returns a
  correct, well-formed JSON 502 — the bug was never in this plugin's
  response body. But 14.5s was slow enough that Cloudflare's tunnel gave
  up on the request FIRST and returned its own error page (plain-text
  "error code: 502" to a bare `curl`, an HTML page to a browser) —
  THAT'S what the operator's browser tried to `.json()`-parse, producing
  "Unexpected token '<'". Nothing wrong with this plugin's JSON; the
  request just never got there fast enough.
- Fix: `TrueNASConnectionError` now carries `retryable` (default `True`).
  `ws_client.connect()` sets it `False` when the transport factory's
  exception is (or mentions) a certificate verification failure —
  `_is_permanent_tls_failure()` checks both `isinstance(...,
  ssl.SSLCertVerificationError)` and a lowercase substring fallback (in
  case a future `websocket-client` version wraps it differently).
  `_connect_with_backoff` now gives up after the FIRST attempt for a
  non-retryable error instead of burning the whole backoff cycle — an
  expired-cert instance now fails in roughly one connection attempt's
  worth of time, not ~15-20s.
- Every OTHER connection failure (refused, timeout, DNS, a transient
  network blip) is unaffected — still retries with the existing backoff.
- 6 new tests (`ws_client`) — 363 tests total.

## [0.10.1] - 2026-07-21 (QA hardening on 0.10.0's parallel_safe_calls)

Fable code audit of 0.10.0 (GO, max severity P3) found two cheap gaps,
both fixed:

- `parallel_safe_calls([])` would have raised `ValueError` from
  `ThreadPoolExecutor(max_workers=0)` — no caller passes an empty list
  today, but the guard costs nothing. Now returns `[]`.
- The non-`TrueNASError` exception path (a real programming bug in one
  spec, e.g. a `TypeError`) was already correct — it propagates exactly
  like a sequential `safe_call` would, never silently swallowed — but had
  no explicit test. Added one.
- 357 tests total (355 + these 2).

## [0.10.0] - 2026-07-21 (perf: parallelize multi-collection reads)

Operator reported the plugin felt slow to load/sync after onboarding a
second instance (`nvmeof-253`) and while chasing an unrelated SSE
hiccup. Root cause for the multi-collection tabs specifically: four
routes each made several INDEPENDENT TrueNAS RPCs back-to-back, paying
one full WebSocket round-trip per call instead of one round-trip total.

- New `core.subsystem.parallel_safe_calls(specs)`: runs several
  `safe_call`-shaped `(label, fn, default)` specs CONCURRENTLY via a
  small `ThreadPoolExecutor`, same isolation semantics as sequential
  `safe_call` (one failing spec still degrades independently, never
  hides the others) — safe because `TrueNASWSClient.call()` is already
  documented "not thread-hostile" (each call gets its own request id),
  the exact property `fleet.py`'s cross-instance fan-out already relied
  on since F3.
- Applied to the four routes that had genuinely independent multi-call
  reads: `shares` (5 calls → 1 round-trip), `apps_vms` (2 → 1),
  `data_protection` (3 → 1), `telemetry` (cpu/memory/interface.query:
  3 → 1, network stays a second stage since it needs the resolved
  interface name first — 4 sequential round-trips down to 2).
- **Not done, considered and rejected**: a server-side TTL cache on the
  remaining read routes (system/pools/datasets/snapshots/etc.), mirroring
  `fleet`'s existing 15s cache. Fleet is a read-only aggregate dashboard
  where slight staleness is harmless; these routes are read right after
  a write (create a dataset, delete a share) and a cache would risk
  showing stale data exactly when the operator is verifying their own
  change. The concurrency fix above addresses per-load latency without
  that trade-off.
- 3 new tests for `parallel_safe_calls` itself (concurrent timing,
  result-order preservation regardless of completion order, per-spec
  failure isolation) — 355 tests total.

## [0.9.0] - 2026-07-20 (F4c: real SMB/NFS share create/update/delete)

Last item of the 4-item batch (charts, F4c, F5, F6 — all done). Schemas
verified live against `.64` before writing any code, same discipline as
every other write phase — but this one touches shares in ACTIVE use by
real clients (a real "nextcloud" SMB share, a real "PBS_NFS" NFS share
backing Proxmox Backup Server), so no write was executed against either;
only `core.get_methods` schemas were inspected.

- `sharing.smb.create/update/delete` and `sharing.nfs.create/update/
  delete` are all synchronous (`job: False`, confirmed live) — no job_id
  handling needed.
- **Deliberately scoped OUT: iSCSI CRUD.** An iSCSI "share" is a 3-way
  join (target + extent + targetextent); building that properly is a
  meaningfully bigger task than SMB/NFS create/update/delete, which covers
  the actual "share a folder without opening TrueNAS" ask. iSCSI stays
  read-only.
- Delete's typed-confirmation guard has one real difference from
  datasets: a dataset's `id` IS a human-readable path, so it confirms
  against itself. An SMB/NFS share's `id` is an opaque integer — the
  builder has no `conn` to look up the real name/path, so the caller (the
  UI, which already has the row) supplies `expected_name`/`expected_path`
  alongside the typed `confirm_name`; the builder only compares the two
  values it's given, never trusts the caller to have gotten
  `expected_name` right.
- **Bug found and fixed while verifying live, unrelated to the write
  path**: this plugin's read-only NFS rendering assumed a `paths` array
  field (`s.paths`) — a guess from F1 that was never live-verified (no
  real NFS share existed to check against at the time). The real field,
  confirmed live against the actual "PBS_NFS" share, is `path` (singular
  string). Fixed alongside the new create/edit/delete UI.
- New "+ Nuevo SMB"/"+ Nuevo NFS" buttons and per-row Editar/Borrar,
  through the same dry-run-preview-then-confirm flow as every other write
  in this plugin.
- 352 tests (up from 332).

## [0.8.0] - 2026-07-20 (Overview telemetry: CPU/memory/network sparklines)

Second-to-last item of the 4-item batch. What the operator asked for after
sharing a screenshot of TrueNAS's own native dashboard, explicitly
deferred until after the Storage grid ("gráficos después") — now built.

Backed by `reporting.get_data`, confirmed live before writing code:
- CPU's `legend` is `['time', 'cpu', 'cpu0', 'cpu1', ...]` — index 1
  ('cpu') is the aggregate/all-core %; the rest are per-core, unused here.
- Memory's `legend` is `['time', 'available']` — bytes still free, NOT a
  used-percentage. Converted using `system.info`'s `physmem` (total
  bytes), fetched once per telemetry request.
- Network needs a real interface `identifier` — passing `None`/`'*'`
  silently returns zero rows (not an error). Resolved via
  `interface.query`'s first configured interface; multi-NIC/bonded setups
  aren't disambiguated in this first pass (the resolved name is returned
  alongside the series so the UI labels it honestly rather than hiding
  the ambiguity).
- A 1-hour window returns ~3600 one-second rows per metric — downsampled
  server-side to at most 120 points before it ever reaches the wire.

`subsystems/telemetry.py`: each series (`safe_call`-isolated, same pattern
as every other multi-call subsystem) so a hung network graph never hides
working CPU/memory. New `GET telemetry` route, fetched alongside
`system`/`pools` by the Overview tab. Hand-rolled SVG sparklines (no
charting library — CT119 has no internet access to fetch one from a CDN):
`renderSparkline`/`renderDualSparkline` draw a `<polyline>` from the
downsampled series; memory is clamped to a fixed 0-100% scale (a
used-percentage auto-scaled like a generic series would exaggerate small
swings into a misleading full-height chart).

- 332 tests (up from 318).

## [0.7.0] - 2026-07-20 (F6: data-protection posture — with a real secret-leak catch)

Third item of the 4-item batch (charts, F4c, F5, F6). Read-only, no
privilege grant needed — the RO key already had `CLOUD_SYNC_READ`/
`SHARING_READ`-adjacent roles that expose `cloudsync.query`/
`rsynctask.query`/`certificate.query`. `smart.test.results` (originally
planned for this phase) was DROPPED after live verification: there is no
`smart.*` method namespace at all on this TrueNAS version — not gated by a
missing role, the API surface doesn't exist. Not built rather than guessed.

**Real finding, not a style choice**: live-verified against `.64` that
`cloudsync.query`'s raw record embeds the cloud provider's credential
**with the secret key in cleartext** (a real Backblaze B2 application key
under `credentials.provider.key`), and `certificate.query`'s raw record
embeds the certificate's **private key in cleartext** (`privatekey`, full
PEM) alongside the public cert/chain/CSR. Every other subsystem in this
plugin deliberately does attrs-passthrough (brief §2) — doing that here
would have leaked a real secret to anyone with `storage.view` on the
PegaProx panel, not just admins. `subsystems/data_protection.py` breaks
that convention on purpose: every function returns an explicit ALLOW-LIST
of fields (schedule/path/enabled/last-run for cloudsync/rsync; name/
common/san/expiry for certificates) and never the raw record. Job
sub-objects (last-run info) are projected too, not embedded whole — a real
job's `logs_excerpt` can run to megabytes (same issue F3's fleet.py hit).
Tests assert the secret strings are ABSENT from the output, not just that
safe fields are present.

- New "Protección" tab: Cloudsync/Rsync task tables (enabled, schedule,
  last run) + Certificates table (common name, expiry, EXPIRADO/OK badge).
- 318 tests (up from 307).

## [0.6.0] - 2026-07-20 (F5: VM start/stop/restart + App start/stop/redeploy)

Part of a 4-item batch the operator approved before publishing the plugin
publicly (charts, F4c shares CRUD, F5, F6 — this entry is F5; the others
land in their own version bumps).

- Verified live (admin session) the real method surface before writing any
  code: `vm.start(id:int, {overcommit})` (sync), `vm.stop(id, {force,
  force_after_timeout})` (job), `vm.restart(id)` (job), `app.start(
  app_name:str)` (job), `app.stop(app_name)` (job), `app.redeploy(
  app_name)` (job). **`app.restart` does not exist** on this TrueNAS
  version — only `redeploy` (stop + pull latest images + start), a
  meaningfully heavier operation. Never aliased to "Reiniciar" in the UI;
  labeled "Redeploy" so the operator isn't misled about what it does.
- Granted `VM_WRITE` + `APPS_WRITE` to the same "PegaProx RW" privilege
  object touched for F4b's `SERVICE_WRITE` — same additive, single-field
  `privilege.update`, nothing else changed. Re-verified live with the RW
  API key that `vm.start/stop/restart` and `app.start/stop/redeploy` are
  now visible under `core.get_methods`.
- **Caveat, stated plainly**: unlike F4b's services (toggled the harmless,
  already-disabled `ftp` service end-to-end), neither `vm.query` nor
  `app.query` had any live row on `.64` at verification time (both `[]` —
  no VM/app configured there yet). The write path reuses the identical,
  already-tested build/execute/verify/audit machinery every other write
  uses; what's NOT independently confirmed is a real VM/app actually
  reaching the expected post-write state on `.64` specifically — re-check
  once one exists there.
- `subsystems/apps_vms.py`: `build_vm_control_envelope`/`control_vm` (VMs
  keyed by integer `id`) and `build_app_control_envelope`/`control_app`
  (apps keyed by string `name`) — same pure-builder/real-caller split as
  every other F2+ write. 6 new `WRITE_OPS` entries reuse the existing
  generic `writes/dry-run`/`writes/execute` routes.
- Apps/VMs tab: per-row Iniciar/Detener/Reiniciar (VMs) and Iniciar/
  Detener/Redeploy (apps) buttons, only the ops valid for the row's
  current state, through the same dry-run-preview-then-confirm flow as
  services/datasets/snapshots.
- 307 tests (up from 286).

## [0.5.0] - 2026-07-20 (F4b: real start/stop/restart of services)

`0.4.0`'s F4a shipped read-only service status because the RW key's
TrueNAS-side privilege lacked `SERVICE_WRITE` (verified live). The operator
had no admin credential for `.64` in the vault to widen it either — solved
by the operator supplying real TrueNAS admin credentials for this instance
specifically, used once to make a single, minimal, verified privilege
change, never persisted anywhere in this repo or CT119 beyond the change
itself:

- Logged in as the real TrueNAS admin (`alfonso`, `FULL_ADMIN`) and found
  the actual object gating this: `privilege` id `5` ("PegaProx RW"), tied
  to the `pegaprox_rw` local group (NOT a field on the API key or the user
  record directly — `user.update`/`api_key.update` have no `roles` field;
  `privilege.update(id, {roles: [...]})` is the real write path).
- Appended `SERVICE_WRITE` to that privilege's existing
  `['DATASET_WRITE', 'DATASET_DELETE', 'SNAPSHOT_WRITE', 'SNAPSHOT_DELETE']`
  — a single-field, additive `privilege.update` call; `local_groups` and
  every other field left untouched. Re-verified live with the RW API key
  itself afterward: `service.start`/`stop`/`restart`/`update` are now
  visible to it under `core.get_methods` (previously invisible).
- `subsystems/services.py`: `build_control_envelope(op, service_name)` +
  `control(conn, op, service_name)`, same pure-builder/real-caller split as
  datasets/snapshots (brief §5) so dry-run and execute can never describe
  a different JSON-RPC call. Each op explicitly passes `{'silent': False}`
  — TrueNAS's own default (`silent: True`) would otherwise turn a failed
  start/stop/restart into an ordinary falsy result instead of a
  `TrueNASRPCError` the write path already knows how to report/audit.
- 3 new `WRITE_OPS` entries (`services.start/stop/restart`) reuse the
  EXISTING generic `writes/dry-run`/`writes/execute` routes — no new route
  needed. `verify()` re-reads the service afterward and checks it actually
  reached the expected state (`RUNNING` for start/restart, `STOPPED` for
  stop), so a `service.stop` that returns success while the service is
  still running surfaces as `verify_failed`, never a false `ok`.
- New "Servicios" tab in the UI: per-service Iniciar/Detener/Reiniciar
  buttons (only the actions valid for the service's CURRENT state), each
  going through the same dry-run-preview-then-confirm flow as datasets/
  snapshots rather than firing on a single click — a stopped SMB/NFS/iSCSI
  service can break something a real client depends on right now.
- 286 tests (up from 273).

## [0.4.0] - 2026-07-20 (F3 Fleet Overview + F4a service status)

Planned with the `arquitecto` (Fable) subagent, then verified live against
a real TrueNAS-25.10.1 instance before writing any code: `service.query`,
`core.get_jobs`, and `audit.query` shapes; `TrueNASWSClient`/
`ConnectionManager` concurrency safety for a multi-instance fan-out; and
the RW key's actual granted role (confirmed it does NOT include
`SERVICE_WRITE`).

- **F3 — Fleet Overview**: new `GET fleet` route + `subsystems/fleet.py`.
  Fans out concurrently (`ThreadPoolExecutor`) over every configured
  instance, combining `system.info` + `alert.list` + `pool.query` +
  `service.query` + a filtered `audit.query` per instance, each RPC
  independently degraded via `safe_call` and each instance isolated so one
  unreachable/hung appliance never blocks or hides the rest. Every
  aggregate (instance health counts, fleet-wide capacity %, top pools by
  usage, merged recent-activity feed) is computed from data the middleware
  actually returns — no invented metric (e.g. no "top memory consumers":
  `system.info` carries no RAM utilization field). TTL-cached server-side
  (15s) so a UI poll tick never re-hammers every appliance. New "Fleet" tab
  in the UI, first in the nav — the one tab that is cross-instance by
  design and never gates on an instance being selected.
  - Recent-activity feed required a live correction mid-design: an
    unfiltered `audit.query` feed is ~100% `AUTHENTICATION`/`LOGOUT`
    self-noise from the plugin's own RO/RW polling logins. Filtering those
    two events out (two ANDed `!=` filters, confirmed live — not the
    untested `nin` operator) surfaces the genuinely actionable entries:
    a human calling TrueNAS's admin UI directly, or the plugin's own RW
    writes.
- **F4a — service status (read-only)**: new `GET services` route +
  `subsystems/services.py` (`service.query`). Flags a service that's
  `enable: true` but not `RUNNING` as unhealthy (a crashed/manually-stopped
  SMB/NFS/iSCSI service an operator would otherwise only discover from a
  client complaining).
- **F4b (start/stop/restart) deliberately NOT implemented**: verified live
  that `service.start`/`stop`/`restart`/`update` are invisible to
  `core.get_methods` under both the current RO key (`SERVICE_READ` only)
  and RW key (granular `DATASET_*`/`SNAPSHOT_*` roles, no `SERVICE_*` at
  all). The real gating role is the builtin `SERVICE_WRITE` — granting it
  to the RW key is a deliberate TrueNAS-side privilege change for the
  operator to decide on, not something this change makes silently.
- 273 tests (up from 246), all green.

## [0.3.1] - 2026-07-20 (QA fable pre-flight — before the first real write against `.64`)

A third QA pass (qa-auditor + silent-failure-hunter, both on Fable, as a
final gate specifically for touching real infrastructure for the first
time) reconfirmed the round-2 fixes are genuinely in the code, ran the
suite/linter independently, and found one non-blocking gap worth closing
before real credentials touch `.64`.

- **Writes now use a 60s timeout (`WRITE_TIMEOUT`), not the 10s read
  default.** A real ZFS write (recursive delete, encrypted/dedup create)
  can legitimately take longer than any read; reusing `DEFAULT_TIMEOUT`
  for writes risked a `TrueNASTimeoutError` reported as `'error'` while
  the write was still genuinely in flight on TrueNAS's side — with no
  poller in F2, an operator retrying a `create` on a false timeout could
  collide with the write that actually succeeded. `datasets.py`/
  `snapshots.py`'s `create`/`update`/`delete` now pass
  `timeout=WRITE_TIMEOUT` explicitly.
- **A late response arriving after its caller already timed out is now
  logged at `warning`, not `debug`, whenever it carries a `result` or an
  `error`.** That frame is the only evidence of whether a timed-out write
  actually landed on TrueNAS; dropping it at debug level the same as any
  harmless late ack made that outcome invisible. A late ack with neither
  key stays at debug — no new noise for the common case.
- 242 tests (up from 235), ruff clean. Both reviews' verdict: **GO** for
  the first real write against `.64` with a dedicated `svc-pegaprox-rw`
  account on a confirmed-ONLINE pool.

## [0.3.0] - 2026-07-20 (post-review hardening, round 2 — write-path)

Two independent reviews (code-reviewer + silent-failure-hunter) audited F2
with more rigor than earlier rounds, being real write-path code. They
confirmed the core architecture holds (genuinely non-divergent dry-run/
execute builder, fail-closed readonly/RW gate, unbypassable confirm_name,
real RO/RW separation) but found 10 real issues, one of which broke the
phase's main feature outright.

- **[Broke the feature] "Confirmar y ejecutar" was permanently disabled
  for dataset CREATE.** `disabled = (op !== 'update')` disabled the button
  for create too; create's confirmation field is hidden, so nothing ever
  re-enabled it — creating a dataset from the UI was literally impossible.
  Fixed to `disabled = (op === 'delete')`.
- **Post-write verify is now exception-proof, and audit is now
  structurally guaranteed.** Previously only `TrueNASError` was caught
  around the verify step; any other exception (AttributeError on an
  unexpected shape, a different timeout type) escaped AFTER a real write
  had already run against TrueNAS, skipped the audit call, and 500'd —
  the operator would see "error" while the dataset/snapshot had actually
  been created/deleted, with zero audit trail. Now wrapped in
  `except Exception`, and `_audit()` runs inside a `finally` so it fires
  no matter what happens computing the final status.
- **Update verify no longer vacuous.** It used to just check "does the
  dataset still exist" — true before AND after any update, so it could
  never actually catch a change that silently didn't apply, and made the
  `'pending'` branch unreachable for updates even on a job-wrapped result.
  Now compares every field in `payload['changes']` against the re-read
  dataset (unwrapping TrueNAS's `{'parsed': ..., 'rawvalue': ...}`
  property shape), excluding `force_size` (a write-only control flag,
  never a persisted property). **Design decision** (field comparison over
  unconditionally forcing `'pending'` on any int result): comparison gives
  a real signal when the write turns out synchronous, and composes with
  the existing job-id logic — a genuine mismatch still yields `'pending'`
  when the result looked like a job id, `'verify_failed'` otherwise.
- **`bool`-as-job-id fixed.** `isinstance(True, int)` is `True` in Python —
  a synchronous write returning `True` with a real verify failure used to
  report `'pending'` (masking a real failure as "still running, check
  later" forever) instead of `'verify_failed'`. Now
  `isinstance(result, int) and not isinstance(result, bool)`.
- **`'verify_error'` split out from `'verify_failed'`.** A verify that
  raised (timeout, dropped connection right after the write) used to
  collapse into the same status as a verify that ran and genuinely
  confirmed the wrong state — for a delete, "still there" (verify_failed)
  and "couldn't check" (verify_error) call for opposite operator
  reactions. Now distinct, with the raw error surfaced in a new
  `verify_error` response field.
- **Pre-execution rejections are now audited too** (`<action>.rejected` —
  readonly, no RW key, bad typed confirmation). A rejected delete attempt
  is exactly the signal an audit trail exists to catch; these previously
  left zero trace.
- **UI: double-submit guard** on every preview/confirm button — none of
  them disabled themselves while a request was in flight, so a double-click
  (or real ZFS-write latency) could fire `writes/execute` twice with no
  server-side idempotency; a second failing call would overwrite the
  first call's success message.
- **UI: malformed JSON in the dataset write form no longer silently
  degrades to `{}`.** A typo in the extra-properties field used to create
  the dataset anyway, minus everything the operator typed, while still
  reporting success. `parseJsonField` now returns an error the caller
  must check before preview/execute proceed.
- **`ConnectionManager` cache key is now a tuple `(instance_id, 'ro'|'rw')`**,
  not a concatenated string — an instance literally named e.g. `'foo::rw'`
  would have collided with the RW-cached client of an instance named
  `'foo'`, cross-wiring privilege/host between two distinct instances.
- **`readonly: null` (hand-edited config.json) now fails closed.**
  `inst.get('readonly', True)` only defaulted a MISSING key to safe; an
  explicit `null` (falsy in Python) slipped through as "not readonly".
  Now `inst.get('readonly') is not False` — anything other than an
  explicit `false` is treated as readonly.
- 235 tests (up from 213), verified via `pytest --collect-only -q`. 94%
  combined coverage on `core/`+`routes/`+`subsystems/` (every module
  ≥91%). Added `tests/test_ui_static.py`: narrow source-pattern regression
  guards for the UI bugs above (this repo has no JS test harness).

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
   issued for `CN=nas-remote.example.com`, not for its LAN IP — connecting by
   IP with `verify_tls: true` failed with "IP address mismatch" even though
   the cert itself is valid. Added `tls_server_name` (optional, per
   instance): overrides the TLS/SNI verification name independently of the
   literal dial host, so the plugin can connect by LAN IP while verifying
   against `nas-remote.example.com`. Threaded through
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
  a free-form `client_id` (`idkmanager`, `acme`, `globex`, `initech`, ...)
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
