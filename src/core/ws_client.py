# -*- coding: utf-8 -*-
"""Generic, reusable JSON-RPC 2.0 client over a persistent WebSocket, for the
TrueNAS SCALE middleware API (``wss://<host>:<port>/api/current``).

Path CONFIRMED 2026-07-20 against a real TrueNAS-25.10.1 instance, twice, in
opposite directions — the first check was wrong; the second reads the
authoritative source and corrects it:

1. First pass (WRONG): reading ``/etc/nginx/nginx.conf`` found ``/websocket``
   as a dedicated, active ``location`` block and concluded it must be the
   real JSON-RPC endpoint. This was a mistake — nginx routing tells you
   which paths reach the backend, not which *application-level protocol*
   each path speaks once it gets there.
2. Second pass (CORRECT — this is the live protocol error that caught it):
   sending a JSON-RPC envelope to ``/websocket`` against the real instance
   produced a server-side crash in ``middlewared``'s log:
   ``middlewared.apps.websocket_app.on_message(): KeyError: 'msg'`` —
   because ``/websocket`` speaks the OLD legacy DDP-style protocol (its
   first expected client message is ``{"msg": "connect", ...}``, not a raw
   JSON-RPC call). Reading ``middlewared/main.py`` directly confirms the
   real dispatch: ``app.router.add_route('GET', '/websocket',
   self.ws_handler)`` is the legacy DDP handler, while
   ``app.router.add_route('GET', f'/api/{version}', RpcWebSocketHandler(...))``
   — registered once per entry in ``self._load_apis()``, which includes the
   key ``"current"`` — is the actual JSON-RPC 2.0 handler
   (``middlewared/api/base/server/ws_handler/rpc.py``). ``/api/current`` was
   the right path from the start.

Design constraints (see PEGAPROX_PLUGIN_TRUENAS_BRIEF.md §2/§4/§9):

- REST v2.0 is deprecated in 25.10 and removed in TrueNAS 26 — this client is
  WebSocket JSON-RPC 2.0 ONLY, from F0.
- Envelope: ``{"jsonrpc": "2.0", "id": N, "method": ..., "params": [...]}``.
  Errors come back as ``response["error"]`` (often nested under
  ``error.data.reason``) — never normalized here (attrs passthrough).
- Auth is a *method call* after the socket is open:
  ``auth.login_with_api_key(["<key>"])``. Never retried automatically and
  never logged.
- Reconnection uses exponential backoff + jitter. A dropped socket does not
  crash anything at import time or at construction time — this module is
  lazy-connect: no network I/O happens until the first ``call()``.
- Retries are for READS only. Callers decide idempotency; this client never
  silently retries a ``call()`` that already reached the server — a timeout
  or connection error is raised to the caller, who is responsible for
  deciding whether to retry a write.

The real transport (``websocket-client``) is imported lazily inside
``_default_transport_factory`` so this module can be imported (and its pure
logic unit-tested) even in an environment where the dependency is not yet
installed — matching the "no external DNS on CT119" constraint from the
brief: importing the plugin must never explode just because a dependency
isn't vendored yet.
"""

import itertools
import json
import logging
import random
import ssl
import threading
import time

from .errors import (
    TrueNASAuthError,
    TrueNASConnectionError,
    TrueNASError,
    TrueNASRPCError,
    TrueNASTimeoutError,
)

log = logging.getLogger('plugin.truenas.ws_client')

DEFAULT_TIMEOUT = 10.0
# Writes (dataset/snapshot create/update/delete) can legitimately take much
# longer than a read (recursive delete, encrypted/dedup create). Reusing
# DEFAULT_TIMEOUT for writes would raise a false TrueNASTimeoutError while
# the write is still genuinely in flight on TrueNAS's side (QA fable finding,
# 2026-07-20, pre real-.64 test) — callers doing a write pass this explicitly.
WRITE_TIMEOUT = 60.0
DEFAULT_MAX_RECONNECT_ATTEMPTS = 5
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_CAP_S = 30.0


def _default_transport_factory(url, verify_tls, timeout, tls_server_name=None):
    """Open a real WebSocket connection using ``websocket-client``.

    Imported lazily so importing this module never requires the dependency
    to be installed (only actually connecting does).

    ``timeout`` governs only the initial TCP/TLS handshake done by
    ``create_connection``. Once open, the socket's recv timeout is cleared
    (``settimeout(None)``) — otherwise an idle-but-perfectly-healthy
    connection would throw ``WebSocketTimeoutException`` out of ``recv()``
    every ``timeout`` seconds, which the reader loop would (correctly, from
    its point of view) treat as a dropped socket: full reconnect + relogin +
    resubscribe churn, and audit-log spam against the appliance, on a
    connection that was never actually unhealthy. Per-request timeouts are
    already enforced independently by ``call()``'s ``Event.wait(timeout)``.

    ``tls_server_name``: real-world TrueNAS instances are commonly reached by
    LAN IP but present a CA-issued cert bound to a DNS name (e.g. an ACME
    cert obtained via DNS-01 for remote-access purposes), so IP-based
    hostname verification fails with "IP address mismatch" even though the
    cert is perfectly valid. Setting this overrides the SNI/verification
    name independently of ``url``'s literal host — dial the IP, verify the
    name. Confirmed live against a real instance 2026-07-20 (cert
    `CN=nas-remote.example.com`, connection host a LAN IP with no DNS relation
    to that name).
    """
    import websocket  # intentional lazy import — see module docstring

    if verify_tls:
        sslopt = {'server_hostname': tls_server_name} if tls_server_name else None
    else:
        sslopt = {'cert_reqs': ssl.CERT_NONE}
    ws = websocket.create_connection(url, timeout=timeout, sslopt=sslopt)
    ws.settimeout(None)
    return ws


_PERMANENT_TLS_FAILURE_MARKERS = (
    'certificate_verify_failed',
    'certificate has expired',
    'certificate is not yet valid',
)


def _is_permanent_tls_failure(exc):
    """True if ``exc`` is (or wraps) a TLS certificate verification failure
    — retrying can never fix an expired/not-yet-valid/untrusted cert, only
    replacing the certificate on the TrueNAS side can. Every OTHER
    connection failure (refused, timeout, DNS, a transient network blip)
    stays retryable.

    Checked two ways: ``isinstance`` against ``ssl.SSLCertVerificationError``
    (the precise, version-stable signal on the versions of Python this
    plugin targets) AND a lowercase substring match on ``str(exc)`` as a
    fallback, in case a future ``websocket-client`` version wraps the SSL
    error in a different exception type without preserving the class —
    found live 2026-07-21: an expired cert made every request to that
    instance retry the full 5-attempt backoff cycle (~15-20s) before
    failing, slow enough that Cloudflare's tunnel returned its own error
    page ahead of this plugin's own (correct) JSON 502.
    """
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _PERMANENT_TLS_FAILURE_MARKERS)


class TrueNASWSClient:
    """Persistent JSON-RPC 2.0 client for one TrueNAS instance's WebSocket.

    Not thread-hostile: ``call()`` may be invoked concurrently from multiple
    threads — each gets its own ``id`` and waits only for its own response.
    """

    def __init__(self, host, port, use_tls=True, verify_tls=False,
                 tls_server_name=None,
                 timeout=DEFAULT_TIMEOUT, transport_factory=None,
                 auto_reconnect=True,
                 max_reconnect_attempts=DEFAULT_MAX_RECONNECT_ATTEMPTS,
                 backoff_base_s=DEFAULT_BACKOFF_BASE_S,
                 backoff_cap_s=DEFAULT_BACKOFF_CAP_S,
                 sleep_fn=time.sleep):
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.verify_tls = verify_tls
        # Overrides TLS/SNI hostname verification independently of `host`
        # (which is what's actually dialed) — see _default_transport_factory
        # docstring: real certs bound to a DNS name, reached by LAN IP.
        self.tls_server_name = tls_server_name
        self.timeout = timeout
        self.auto_reconnect = auto_reconnect
        self.max_reconnect_attempts = max_reconnect_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self._sleep = sleep_fn
        self._transport_factory = transport_factory or _default_transport_factory

        self._ws = None
        self._connected = False
        self._connect_lock = threading.RLock()
        self._send_lock = threading.Lock()

        # Set by an explicit close(); checked by _connect_with_backoff before
        # every (re)connect attempt (including right after a backoff sleep)
        # so a close() that races an in-flight background reconnect actually
        # cancels it, instead of the reconnect thread waking up and silently
        # opening a fresh socket authenticated with the old api_key. A later
        # explicit connect() clears it again (intentional reopen).
        self._closed = False

        # Non-blocking guard: at most one _background_reconnect worker at a
        # time per client. Without this, a second unexpected drop arriving
        # while the first recovery is still mid-relogin would spawn a
        # duplicate worker -> duplicate core.subscribe calls.
        self._reconnecting = False
        self._reconnect_guard_lock = threading.Lock()

        # Guards the check-then-login sequence end to end (see
        # ensure_logged_in) — NOT just against two callers racing each
        # other, but against ANY second auth.login_with_api_key on the same
        # socket generation, concurrent OR sequential. Found live
        # 2026-07-21: TrueNAS's own auth state machine leaves the "LOGIN"
        # state after the first successful login on a session, so a SECOND
        # login call (e.g. a caller's explicit login() landing while
        # _background_reconnect's own relogin is still in flight — or even
        # right after it finished) is rejected with "unexpected
        # authenticator run state", which _do_login (wrongly, for this
        # specific case) treats as a bad/revoked key and poisons
        # needs_auth permanently. Always acquired BEFORE _connect_lock,
        # never the reverse, everywhere in this file — no cycle, no
        # deadlock risk (verified by inspection: _teardown_socket,
        # _handle_unexpected_disconnect, and the reader thread never touch
        # this lock, so a socket dropping mid-login cannot deadlock it).
        self._login_lock = threading.Lock()

        # True once a relogin-after-reconnect was rejected by the server
        # (bad/revoked key) rather than failing transiently — the caller
        # (conn_manager / routes) can surface this instead of a generic
        # "not connected".
        self.needs_auth = False

        self._id_seq = itertools.count(1)
        self._id_lock = threading.Lock()

        self._pending = {}          # req_id -> {'event': Event, 'response': dict|None}
        self._pending_lock = threading.Lock()

        self._subscriptions = {}    # event_name -> [callback, ...]
        self._subscriptions_lock = threading.Lock()

        self._api_key = None        # kept only to support relogin after reconnect
        # Distinct from `self._api_key is not None`: the key is kept around
        # across a torn-down socket so a reconnect can relogin, but the
        # CURRENT socket's session is gone the moment it's torn down. F1's
        # subsystem routes gate their first call per request on
        # `is_authenticated`, so this must go False exactly when the live
        # session actually stops being valid, not just when we forget the
        # key.
        self._authenticated = False
        self._last_error = None

        self._reader_thread = None
        self._stop_reader = threading.Event()

    # -- public state -------------------------------------------------------

    @property
    def is_connected(self):
        return self._connected

    @property
    def is_authenticated(self):
        """True once ``login()``/``_do_login()`` succeeded on the CURRENT
        socket. Goes False on any teardown (close(), a torn-down failed
        relogin, or an unexpected disconnect) — a fresh reconnect always
        needs a fresh login even though ``_api_key`` is retained for the
        auto-relogin path."""
        return self._authenticated

    @property
    def last_error(self):
        return self._last_error

    def url(self):
        scheme = 'wss' if self.use_tls else 'ws'
        return f'{scheme}://{self.host}:{self.port}/api/current'

    # -- connection lifecycle ------------------------------------------------

    def connect(self, _clear_closed=True):
        """Open the WebSocket if not already connected. Idempotent.

        Raises ``TrueNASConnectionError`` on failure; never raises on an
        already-open connection.

        ``_clear_closed`` is internal plumbing, not a public parameter for
        callers to pass. Only a direct, explicit ``connect()`` call (the
        default, ``_clear_closed=True``) is allowed to clear a prior
        ``close()`` and reopen the client. ``_connect_with_backoff`` —
        shared by lazy-connect (``_ensure_connected``) AND the background
        reconnect worker — always calls ``self.connect(_clear_closed=False)``
        so the "is this client closed?" check and the actual (re)connect
        happen ATOMICALLY under this same lock acquisition.

        This closes a residual TOCTOU: an earlier fix made ``close()`` set
        ``self._closed`` and had callers check it BEFORE calling
        ``connect()`` — but a ``close()`` from another thread landing in the
        gap between that check and this method acquiring ``_connect_lock``
        would still let the reconnect worker open a fresh socket and
        relogin with the stale ``_api_key``, exactly the resurrection bug
        this design exists to prevent. Folding the check inside the lock
        removes the gap entirely.
        """
        with self._connect_lock:
            if self._connected:
                return
            if self._closed and not _clear_closed:
                raise TrueNASConnectionError(
                    'client was closed; refusing to reconnect without an explicit connect()')
            try:
                self._ws = self._transport_factory(
                    self.url(), self.verify_tls, self.timeout, self.tls_server_name)
            except Exception as e:
                self._last_error = str(e)
                raise TrueNASConnectionError(
                    f'could not connect to {self.host}:{self.port}: {e}',
                    retryable=not _is_permanent_tls_failure(e)) from e
            self._connected = True
            if _clear_closed:
                self._closed = False  # explicit (re)connect always clears a prior close()
            self._last_error = None
            self._stop_reader.clear()
            self._reader_thread = threading.Thread(
                target=self._read_loop, name=f'truenas-ws-{self.host}', daemon=True)
            self._reader_thread.start()
            log.info(f'[truenas] connected to {self.host}:{self.port}')

    def close(self):
        """User/operator-initiated close: tears down the socket AND cancels
        any in-flight/future automatic reconnect (``self._closed``)."""
        with self._connect_lock:
            self._closed = True
        self._teardown_socket('connection closed')

    def _teardown_socket(self, reason):
        """Tear down the current transport and fail any pending calls,
        WITHOUT touching ``self._closed``. Used both by ``close()`` (which
        sets ``_closed`` itself beforehand) and internally by
        ``_relogin_and_resubscribe`` to discard a socket that connected but
        failed to (re)authenticate — that case must NOT set ``_closed``, or
        the bounded retry loop in ``_background_reconnect`` would abort
        after a single failed cycle instead of retrying up to
        ``max_reconnect_attempts`` times."""
        with self._connect_lock:
            self._stop_reader.set()
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
            self._connected = False
            self._authenticated = False
            self._last_error = reason
        self._fail_all_pending(reason)

    def _ensure_connected(self):
        if self._connected:
            return
        self._connect_with_backoff()

    def _connect_with_backoff(self, max_attempts=None):
        """Try ``connect()`` repeatedly with exponential backoff + jitter.

        Raises ``TrueNASConnectionError`` (the last failure) once
        ``max_attempts`` is exhausted. Backoff delay is
        ``min(cap, base * 2**attempt) * (0.5 + random())`` — capped jitter,
        never a thundering herd against a struggling appliance.

        Always calls ``connect(_clear_closed=False)`` — this is the
        internal path (shared by lazy-connect and the background reconnect
        worker), never a direct user call, so it must never resurrect a
        client the user explicitly closed. The ``_closed`` check itself
        happens atomically INSIDE ``connect()``'s lock, not here — a
        separate pre-check here would reopen the exact TOCTOU window
        ``connect()`` closes (see its docstring). The ``self._closed``
        check below the exception is just a fast-path to skip a pointless
        backoff sleep once we already know why the last attempt failed; it
        is NOT the correctness guarantee.
        """
        attempts = max_attempts if max_attempts is not None else self.max_reconnect_attempts
        last_exc = None
        for attempt in range(attempts):
            try:
                self.connect(_clear_closed=False)
                return
            except TrueNASConnectionError as e:
                last_exc = e
                if self._closed:
                    raise
                if not e.retryable:
                    # A permanent failure (e.g. an expired TLS cert) will
                    # fail identically on every remaining attempt — bail
                    # immediately instead of paying the full backoff cycle
                    # (up to ~15-20s) for a retry that can never succeed.
                    log.warning(f'[truenas] connect to {self.host}:{self.port} failed '
                                f'with a non-retryable error, giving up immediately: {e}')
                    raise
                if attempt < attempts - 1:
                    delay = min(self.backoff_cap_s, self.backoff_base_s * (2 ** attempt))
                    delay *= (0.5 + random.random())
                    log.warning(f'[truenas] reconnect attempt {attempt + 1}/{attempts} '
                                f'failed, retrying in {delay:.1f}s: {e}')
                    self._sleep(delay)
        raise last_exc or TrueNASConnectionError('reconnect failed for unknown reason')

    def _relogin_and_resubscribe(self):
        """After an automatic reconnect, re-login (if we had a session) and
        re-subscribe to every previously-registered event name — otherwise
        jobs/notifications the UI is watching would go silently orphaned.

        On failure, the socket is torn down via ``_teardown_socket`` (never
        left half-alive reporting ``is_connected == True`` with an
        unauthenticated/unsubscribed session) and the real cause is
        recorded in ``last_error`` — this does NOT set ``self._closed``,
        so a transient failure can still be retried by
        ``_background_reconnect``. Re-raises
        so ``_background_reconnect`` can tell an auth rejection (give up,
        set ``needs_auth``) apart from a transient failure (retry the whole
        connect+relogin cycle).

        Goes through ``ensure_logged_in`` (not a direct ``_do_login`` call)
        for the exact same reason every other caller of the shared cached
        client must: an external caller's ``ensure_logged_in`` racing this
        method — or landing right after it already finished — must never
        trigger a second, redundant ``auth.login_with_api_key`` on the
        already-authenticated session (see ``ensure_logged_in``'s
        docstring for the live incident this fixes).
        """
        if self._api_key:
            try:
                self.ensure_logged_in(self._api_key)
            except TrueNASAuthError as e:
                log.error(f'[truenas] relogin after reconnect rejected by server '
                          f'(bad/revoked key?): {e}')
                self.needs_auth = True
                self._teardown_socket(str(e))
                raise
            except TrueNASError as e:
                log.warning(f'[truenas] relogin after reconnect failed transiently: {e}')
                self._teardown_socket(str(e))
                raise
        with self._subscriptions_lock:
            names = list(self._subscriptions.keys())
        for name in names:
            try:
                self.call('core.subscribe', [name])
            except TrueNASError as e:
                log.warning(f'[truenas] re-subscribe to {name} failed: {e}')

    def _handle_unexpected_disconnect(self, reason):
        with self._connect_lock:
            self._connected = False
            self._authenticated = False
            self._last_error = reason
        self._fail_all_pending(reason)
        log.warning(f'[truenas] socket to {self.host}:{self.port} dropped: {reason}')
        if not self.auto_reconnect:
            return
        with self._reconnect_guard_lock:
            if self._reconnecting:
                log.debug(f'[truenas] reconnect already in progress for '
                          f'{self.host}:{self.port}, not spawning a duplicate')
                return
            self._reconnecting = True
        threading.Thread(target=self._background_reconnect,
                          name=f'truenas-reconnect-{self.host}', daemon=True).start()

    def _background_reconnect(self):
        """Recovery path after an *unexpected* drop (see
        ``_handle_unexpected_disconnect``) — reconnects and then, unlike a
        plain lazy first connect, re-logs-in and re-subscribes so an
        already-established session doesn't silently lose its jobs feed.
        NOT used by ``_ensure_connected()``'s ordinary lazy-connect path —
        calling ``_relogin_and_resubscribe`` there would recurse into
        ``call()`` from inside the very ``login()``/``subscribe()`` that
        triggered the first connect.

        Only one worker runs at a time per client (``_reconnecting`` guard,
        set by the caller before spawning this). Retries the FULL
        connect+relogin cycle (with backoff) on a transient relogin
        failure — a bare reconnect without relogin would leave the socket
        open but unauthenticated. Gives up immediately on an auth rejection
        (``needs_auth`` stays set) rather than hammering a revoked key.
        """
        try:
            for cycle in range(self.max_reconnect_attempts):
                try:
                    self._connect_with_backoff()
                except TrueNASConnectionError as e:
                    log.error(f'[truenas] gave up reconnecting to {self.host}:{self.port}: {e}')
                    return
                try:
                    self._relogin_and_resubscribe()
                    return
                except TrueNASAuthError:
                    return  # needs_auth already set; do not hammer a bad key
                except TrueNASError as e:
                    if cycle >= self.max_reconnect_attempts - 1:
                        log.error(f'[truenas] gave up relogging in to '
                                  f'{self.host}:{self.port} after {cycle + 1} cycles: {e}')
                        return
                    delay = min(self.backoff_cap_s, self.backoff_base_s * (2 ** cycle))
                    delay *= (0.5 + random.random())
                    self._sleep(delay)
                    if self._closed:
                        return
        finally:
            with self._reconnect_guard_lock:
                self._reconnecting = False

    # -- id / pending bookkeeping ---------------------------------------------

    def _next_id(self):
        with self._id_lock:
            return next(self._id_seq)

    def _fail_all_pending(self, reason):
        with self._pending_lock:
            items = list(self._pending.items())
            self._pending.clear()
        for _req_id, entry in items:
            entry['response'] = None
            entry['error_reason'] = reason
            entry['event'].set()

    # -- request/response -----------------------------------------------------

    def call(self, method, params=None, timeout=None):
        """Issue a JSON-RPC 2.0 call and block for its matching response.

        Returns ``response['result']``. Raises:
          - ``TrueNASConnectionError`` if the socket cannot be (re)established
            or drops before a response arrives.
          - ``TrueNASTimeoutError`` if no response for this ``id`` arrives
            within ``timeout`` seconds.
          - ``TrueNASRPCError`` if the response carries a JSON-RPC ``error``.
        """
        timeout = self.timeout if timeout is None else timeout
        self._ensure_connected()

        req_id = self._next_id()
        entry = {'event': threading.Event(), 'response': None, 'error_reason': None}
        with self._pending_lock:
            self._pending[req_id] = entry

        envelope = {'jsonrpc': '2.0', 'id': req_id, 'method': method, 'params': params or []}
        try:
            self._send(json.dumps(envelope))
        except Exception as e:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TrueNASConnectionError(f'send failed for {method}: {e}') from e

        if not entry['event'].wait(timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TrueNASTimeoutError(f"timeout ({timeout}s) waiting for '{method}' response")

        response = entry['response']
        if response is None:
            raise TrueNASConnectionError(
                entry['error_reason'] or f"connection lost before '{method}' answered")

        error = response.get('error')
        if error:
            raise TrueNASRPCError(method, error)
        return response.get('result')

    def _send(self, raw):
        with self._send_lock:
            if not self._connected or self._ws is None:
                raise TrueNASConnectionError('not connected')
            self._ws.send(raw)

    def _read_loop(self):
        while not self._stop_reader.is_set():
            try:
                raw = self._ws.recv()
            except Exception as e:
                if not self._stop_reader.is_set():
                    self._handle_unexpected_disconnect(str(e))
                return
            try:
                self._handle_raw_frame(raw)
            except Exception as e:
                # A malformed-but-not-quite-JSON-broken frame (e.g. a dict
                # whose 'params' is a list instead of a dict, or any other
                # shape the middleware sends that this client doesn't
                # anticipate) must NOT silently kill this thread — that
                # would leave ``is_connected`` stuck True with nobody ever
                # reading from the socket again, and every future call()
                # dying by generic timeout with no visible cause. Treat it
                # exactly like a dropped connection: log loudly, fail
                # pending calls, and let auto-reconnect take over.
                log.exception(f'[truenas] reader loop crashed handling a frame from '
                               f'{self.host}:{self.port}, treating as a dropped connection: {e}')
                if not self._stop_reader.is_set():
                    self._handle_unexpected_disconnect(f'reader crashed: {e}')
                return

    def _handle_raw_frame(self, raw):
        if not raw:
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            log.warning('[truenas] discarding non-JSON frame from socket')
            return
        if not isinstance(msg, dict):
            log.warning(f'[truenas] discarding non-object JSON frame from socket: {msg!r}')
            return
        if msg.get('id') is not None:
            self._dispatch_response(msg)
        elif msg.get('id') is None and msg.get('error') is not None:
            # A JSON-RPC error with id: null is a protocol-level failure the
            # server couldn't attribute to any specific request. It cannot
            # be matched to a pending call() (there's no id to match), so
            # without this branch it silently fell into the notification
            # path and vanished — the caller just saw a generic timeout with
            # no clue why. Surface it loudly instead.
            log.warning(f'[truenas] server sent a protocol-level error (id=null) from '
                        f'{self.host}:{self.port}: {msg.get("error")}')
        else:
            self._dispatch_notification(msg)

    def _dispatch_response(self, msg):
        req_id = msg.get('id')
        with self._pending_lock:
            entry = self._pending.pop(req_id, None)
        if entry is None:
            # An orphaned response (its call() already gave up on timeout) is
            # normally harmless for a read. For a write it's the ONLY
            # evidence of whether the write actually landed on TrueNAS after
            # this client stopped waiting — losing it at debug level would
            # make a real create/delete outcome invisible (QA fable finding,
            # 2026-07-20). Escalate whenever the frame carries a result or
            # an error; a bare ack with neither stays at debug.
            if 'result' in msg or msg.get('error') is not None:
                log.warning(f"[truenas] late response for expired id={req_id} carried "
                            f"a result/error the caller already timed out on: {msg}")
            else:
                log.debug(f'[truenas] response for unknown/expired id={req_id}, dropping')
            return
        entry['response'] = msg
        entry['event'].set()

    def _dispatch_notification(self, msg):
        """Route an unsolicited server message (job/event notification) to
        subscribers. TrueNAS's exact collection_update envelope shape is
        deferred to F1 (jobs.py) — F0 only guarantees callbacks are invoked
        with the raw decoded message for whichever ``method``/collection
        name they subscribed to, never crashing the reader loop if a
        callback misbehaves. Defensive against non-dict ``msg``/``params``
        even though ``_handle_raw_frame`` already filters those out — this
        method must stay safe to call directly too."""
        if not isinstance(msg, dict):
            log.warning(f'[truenas] discarding non-dict notification: {msg!r}')
            return
        params = msg.get('params')
        name = msg.get('method') or (params.get('msg') if isinstance(params, dict) else None)
        with self._subscriptions_lock:
            callbacks = list(self._subscriptions.get(name, []))
        for cb in callbacks:
            try:
                cb(msg)
            except Exception as e:
                log.error(f'[truenas] subscriber callback for {name!r} raised: {e}')

    # -- auth -----------------------------------------------------------------

    def login(self, api_key):
        """Authenticate the just-opened socket. Never logs the key itself.
        Always attempts the login unconditionally — correct for a
        throwaway, never-cached client (e.g. ``conn_manager.test_connection``'s
        one-shot "Probar conexión" check), where there's no shared session
        state anyone else could be racing. For the SHARED, per-instance
        CACHED client (``conn_manager.get_connection``'s result, used by
        every read/write route and now the F4a poller), callers must use
        ``ensure_logged_in`` instead — see its docstring for why."""
        return self._do_login(api_key)

    def ensure_logged_in(self, api_key):
        """Atomic check-then-login for a SHARED, cached client: skips the
        login call entirely if another thread already authenticated this
        exact socket generation while we were waiting for ``_login_lock``.

        This is NOT just "avoid two threads sending auth.login_with_api_key
        at once" — TrueNAS's own auth state machine rejects a SECOND login
        on the same session even sequentially, once the first one already
        succeeded (found live 2026-07-21: a route's explicit login landing
        right after ``_background_reconnect``'s own relogin already
        finished still got "unexpected authenticator run state", not a
        race in the concurrent sense — just a redundant call this method
        exists to prevent). Every caller that shares a cached client
        (``routes/api.py``'s ``_get_authenticated_connection`` /
        ``_get_rw_authenticated_connection``, and ``_relogin_and_resubscribe``
        below) MUST go through this, never call ``login()``/``_do_login()``
        directly, or the same double-login rejection resurfaces.

        Raises ``TrueNASAuthError`` immediately if ``needs_auth`` is
        already set (a previous attempt was rejected) — never retries a
        doomed call under the lock."""
        with self._login_lock:
            if self.needs_auth:
                raise TrueNASAuthError('auth.login_with_api_key', {
                    'message': 'API key was rejected on a previous attempt — '
                               'check/rotate it in Settings before retrying'})
            if not self.is_authenticated:
                self._do_login(api_key)

    def _do_login(self, api_key):
        try:
            result = self.call('auth.login_with_api_key', [api_key])
        except TrueNASRPCError as e:
            # Set needs_auth here too, not only in _relogin_and_resubscribe's
            # reconnect-triggered path — otherwise a bad key rejected on the
            # very FIRST explicit login() (e.g. routes/api.py's
            # _get_authenticated_connection, called on a persistent,
            # per-instance-cached connection) would never flip this flag,
            # and every subsequent request/poll would retry the identical
            # doomed login call against the appliance forever.
            self.needs_auth = True
            raise TrueNASAuthError('auth.login_with_api_key', e.error) from e
        if result is False:
            self.needs_auth = True
            raise TrueNASAuthError('auth.login_with_api_key', {'message': 'rejected'})
        self._api_key = api_key
        self.needs_auth = False
        # Assign _authenticated atomically under _connect_lock, gated on the
        # socket STILL being the one that just answered the login call. If
        # the socket dropped in the window between call() returning and
        # this line running, the reader thread already set
        # _authenticated=False (and _connected=False) under this same lock
        # — writing True here unconditionally would silently overwrite that
        # with a lie: _connected=False but is_authenticated=True, so the
        # NEXT request would skip login() entirely against a fresh,
        # unauthenticated socket. Same "state that lies" bug class this
        # file already fixed three times for _connected/_closed in F0.
        with self._connect_lock:
            if not self._connected:
                raise TrueNASConnectionError(
                    'socket dropped during login — session not established')
            self._authenticated = True
        return result

    # -- events (hook for F1: core.get_jobs) -----------------------------------

    def subscribe(self, name, callback=None):
        """Subscribe to a server-side event/collection name (e.g.
        ``core.get_jobs``). Registers ``callback`` for later dispatch by
        ``_dispatch_notification`` and issues ``core.subscribe`` on the wire.
        Not exercised by any F0 route — the interface is prepared for F1's
        job-tracking, per the brief."""
        with self._subscriptions_lock:
            self._subscriptions.setdefault(name, [])
            if callback is not None:
                self._subscriptions[name].append(callback)
        return self.call('core.subscribe', [name])

    def unsubscribe(self, name, callback=None):
        # TODO(F1): this only stops LOCAL dispatch — it never sends
        # core.unsubscribe on the wire, so the server keeps pushing
        # collection_update events for `name` that we now just drop. Harmless
        # in F0 (nothing calls unsubscribe(); subscribe() itself is unused by
        # any route yet), but wire the real core.unsubscribe call once F1's
        # job-tracking actually uses subscribe/unsubscribe in anger.
        with self._subscriptions_lock:
            if name not in self._subscriptions:
                return
            if callback is None:
                self._subscriptions.pop(name, None)
            else:
                self._subscriptions[name] = [c for c in self._subscriptions[name] if c != callback]
