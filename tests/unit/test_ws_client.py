# -*- coding: utf-8 -*-
"""Unit tests for core.ws_client.TrueNASWSClient — request/response framing,
concurrent ``id`` handling, timeout, JSON-RPC error propagation and
reconnection backoff. No real network: a FakeTransport drives ``send``/
``recv`` through an in-memory queue."""

import json
import logging
import queue
import sys
import threading
import time
import types

import pytest

from core.errors import (
    TrueNASAuthError,
    TrueNASConnectionError,
    TrueNASRPCError,
    TrueNASTimeoutError,
)
from core.ws_client import TrueNASWSClient, _is_permanent_tls_failure


class FakeTransport:
    """Stand-in for the object ``websocket.create_connection`` returns."""

    def __init__(self):
        self._inbox = queue.Queue()
        self.sent = []
        self.closed = False

    def send(self, raw):
        self.sent.append(raw)

    def recv(self):
        item = self._inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True

    def push(self, obj):
        self._inbox.put(json.dumps(obj) if not isinstance(obj, str) else obj)

    def push_error(self, exc):
        self._inbox.put(exc)


def _wait_for(predicate, timeout=2.0):
    """Poll ``predicate`` until true or timeout — condition-based waiting,
    never an arbitrary sleep."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _shutdown(client, ft):
    """Unblock a FakeTransport stuck in queue.get() so the reader daemon
    thread can observe stop_reader and exit cleanly."""
    client.close()
    ft.push({'jsonrpc': '2.0', 'method': 'noop', 'params': {}})


def _client_with(ft, **kwargs):
    return TrueNASWSClient('truenas.example', 443,
                            transport_factory=lambda *a, **k: ft, **kwargs)


# ---------------------------------------------------------------------------
# Framing: request envelope + matching response by id
# ---------------------------------------------------------------------------

def test_call_sends_correct_jsonrpc_envelope():
    ft = FakeTransport()
    client = _client_with(ft)
    result_box = {}

    def do_call():
        result_box['result'] = client.call('system.info', ['a', 'b'])

    t = threading.Thread(target=do_call)
    t.start()
    assert _wait_for(lambda: ft.sent)
    req = json.loads(ft.sent[0])
    assert req['jsonrpc'] == '2.0'
    assert req['method'] == 'system.info'
    assert req['params'] == ['a', 'b']
    assert isinstance(req['id'], int)

    ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': {'ok': True}})
    t.join(timeout=2)
    assert result_box['result'] == {'ok': True}
    _shutdown(client, ft)


def test_call_defaults_params_to_empty_list():
    ft = FakeTransport()
    client = _client_with(ft)

    def do_call():
        client.call('core.get_jobs')

    t = threading.Thread(target=do_call)
    t.start()
    assert _wait_for(lambda: ft.sent)
    req = json.loads(ft.sent[0])
    assert req['params'] == []
    ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': []})
    t.join(timeout=2)
    _shutdown(client, ft)


def test_concurrent_calls_get_matched_to_their_own_id():
    ft = FakeTransport()
    client = _client_with(ft)
    results = {}

    def do_call(name):
        results[name] = client.call(f'method.{name}', [name])

    threads = [threading.Thread(target=do_call, args=(n,)) for n in ('a', 'b', 'c')]
    for t in threads:
        t.start()
    assert _wait_for(lambda: len(ft.sent) == 3)

    # Answer out of order to prove dispatch is by id, not send order.
    reqs = [json.loads(s) for s in ft.sent]
    for req in reversed(reqs):
        ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': f"result-for-{req['params'][0]}"})

    for t in threads:
        t.join(timeout=2)
    assert results == {'a': 'result-for-a', 'b': 'result-for-b', 'c': 'result-for-c'}
    _shutdown(client, ft)


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

def test_call_raises_timeout_when_no_response_arrives():
    ft = FakeTransport()
    client = _client_with(ft)
    with pytest.raises(TrueNASTimeoutError):
        client.call('system.info', timeout=0.05)
    _shutdown(client, ft)


def test_late_response_after_timeout_is_logged_loudly_when_it_carries_a_result(caplog):
    """Regression (QA fable, 2026-07-20, pre real-.64 test): once a caller
    times out, the ``req_id`` entry is already popped from ``_pending`` —
    if TrueNAS's answer to a WRITE arrives after that, it's the only
    evidence of whether the write actually applied. Silently dropping it at
    debug level (as any other harmless late ack) would make that outcome
    invisible; it must escalate to warning whenever the frame carries a
    result or an error."""
    ft = FakeTransport()
    client = _client_with(ft)
    with pytest.raises(TrueNASTimeoutError):
        client.call('pool.dataset.delete', ['tank/test-dataset'], timeout=0.05)
    req = json.loads(ft.sent[0])

    with caplog.at_level(logging.WARNING, logger='plugin.truenas.ws_client'):
        ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': True})
        assert _wait_for(lambda: any('late response' in r.message for r in caplog.records))
    _shutdown(client, ft)


def test_late_response_after_timeout_stays_quiet_when_it_is_a_bare_ack(caplog):
    """A late response with neither 'result' nor 'error' (an unusual but
    harmless shape) must not spam a warning for every ordinary expired-id
    frame — only ones that actually carry a write outcome."""
    ft = FakeTransport()
    client = _client_with(ft)
    with pytest.raises(TrueNASTimeoutError):
        client.call('system.info', timeout=0.05)
    req = json.loads(ft.sent[0])

    with caplog.at_level(logging.DEBUG, logger='plugin.truenas.ws_client'):
        ft.push({'jsonrpc': '2.0', 'id': req['id']})
        assert _wait_for(lambda: any('unknown/expired' in r.message for r in caplog.records))
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    _shutdown(client, ft)


# ---------------------------------------------------------------------------
# JSON-RPC error propagation
# ---------------------------------------------------------------------------

def test_call_raises_rpc_error_when_response_has_error_field():
    ft = FakeTransport()
    client = _client_with(ft)
    result_box = {}

    def do_call():
        try:
            client.call('pool.query')
        except TrueNASRPCError as e:
            result_box['exc'] = e

    t = threading.Thread(target=do_call)
    t.start()
    assert _wait_for(lambda: ft.sent)
    req = json.loads(ft.sent[0])
    ft.push({'jsonrpc': '2.0', 'id': req['id'],
              'error': {'data': {'reason': 'Pool not found'}}})
    t.join(timeout=2)
    assert isinstance(result_box.get('exc'), TrueNASRPCError)
    assert 'Pool not found' in str(result_box['exc'])
    _shutdown(client, ft)


def test_login_failure_raises_auth_error_without_leaking_key():
    ft = FakeTransport()
    client = _client_with(ft)
    result_box = {}

    def do_login():
        try:
            client.login('super-secret-api-key')
        except TrueNASAuthError as e:
            result_box['exc'] = e

    t = threading.Thread(target=do_login)
    t.start()
    assert _wait_for(lambda: ft.sent)
    req = json.loads(ft.sent[0])
    assert req['method'] == 'auth.login_with_api_key'
    assert req['params'] == ['super-secret-api-key']
    ft.push({'jsonrpc': '2.0', 'id': req['id'],
              'error': {'message': 'invalid api key'}})
    t.join(timeout=2)
    exc = result_box.get('exc')
    assert isinstance(exc, TrueNASAuthError)
    assert 'super-secret-api-key' not in str(exc)
    assert client.needs_auth is True
    _shutdown(client, ft)


def test_first_login_rejection_sets_needs_auth_not_only_reconnect_relogin():
    """needs_auth used to only be set from _relogin_and_resubscribe's
    reconnect-triggered path — a bad key rejected on the very FIRST
    explicit login() (the common case for a fresh, persistent,
    per-instance-cached connection) never flipped it, so every subsequent
    request/poll retried the identical doomed login call forever."""
    ft = FakeTransport()
    client = _client_with(ft)
    assert client.needs_auth is False

    def do_login():
        try:
            client.login('bad-key')
        except TrueNASAuthError:
            pass

    t = threading.Thread(target=do_login)
    t.start()
    assert _wait_for(lambda: ft.sent)
    req = json.loads(ft.sent[0])
    ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': False})
    t.join(timeout=2)

    assert client.needs_auth is True
    _shutdown(client, ft)


def test_successful_login_clears_needs_auth():
    ft = FakeTransport()
    client = _client_with(ft)
    client.needs_auth = True  # simulate a prior rejection

    def do_login():
        client.login('good-key-now')

    t = threading.Thread(target=do_login)
    t.start()
    assert _wait_for(lambda: ft.sent)
    req = json.loads(ft.sent[0])
    ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': True})
    t.join(timeout=2)

    assert client.needs_auth is False
    _shutdown(client, ft)


# ---------------------------------------------------------------------------
# Reconnection: exponential backoff + jitter
# ---------------------------------------------------------------------------

def test_connect_with_backoff_retries_then_succeeds():
    ft = FakeTransport()
    attempts = {'n': 0}

    def factory(url, verify_tls, timeout, tls_server_name=None):
        attempts['n'] += 1
        if attempts['n'] < 3:
            raise RuntimeError('connection refused (simulated)')
        return ft

    client = TrueNASWSClient('truenas.example', 443, transport_factory=factory,
                              sleep_fn=lambda s: None)  # skip real backoff delay
    client._connect_with_backoff(max_attempts=5)
    assert client.is_connected
    assert attempts['n'] == 3
    _shutdown(client, ft)


def test_connect_with_backoff_raises_after_exhausting_attempts():
    def factory(url, verify_tls, timeout, tls_server_name=None):
        raise RuntimeError('connection refused (simulated)')

    client = TrueNASWSClient('truenas.example', 443, transport_factory=factory,
                              sleep_fn=lambda s: None)
    with pytest.raises(TrueNASConnectionError):
        client._connect_with_backoff(max_attempts=3)
    assert not client.is_connected


# ---------------------------------------------------------------------------
# Permanent TLS failures (expired/invalid cert) fail fast, no backoff burn
# ---------------------------------------------------------------------------

def test_expired_certificate_gives_up_after_a_single_attempt_no_sleep():
    """Found live 2026-07-21: retrying an expired cert 5 times with growing
    backoff turned one permanently-broken instance into a ~15-20s wait on
    EVERY request to it — slow enough that Cloudflare's tunnel returned its
    own error page before this plugin's own (correct) JSON 502 ever
    arrived. A cert failure can never be fixed by retrying, so it must not
    pay the backoff loop at all."""
    import ssl

    attempts = {'n': 0}
    sleeps = []

    def factory(url, verify_tls, timeout, tls_server_name=None):
        attempts['n'] += 1
        raise ssl.SSLCertVerificationError('certificate verify failed: certificate has expired')

    client = TrueNASWSClient('truenas.example', 443, transport_factory=factory,
                              sleep_fn=lambda s: sleeps.append(s))
    with pytest.raises(TrueNASConnectionError):
        client._connect_with_backoff(max_attempts=5)
    assert attempts['n'] == 1
    assert sleeps == []


def test_non_ssl_exception_mentioning_expired_certificate_also_fails_fast():
    """String-fallback path: some transport versions may wrap the SSL error
    in a different exception type without preserving the class."""
    attempts = {'n': 0}

    def factory(url, verify_tls, timeout, tls_server_name=None):
        attempts['n'] += 1
        raise RuntimeError('[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired')

    client = TrueNASWSClient('truenas.example', 443, transport_factory=factory,
                              sleep_fn=lambda s: None)
    with pytest.raises(TrueNASConnectionError):
        client._connect_with_backoff(max_attempts=5)
    assert attempts['n'] == 1


def test_is_permanent_tls_failure_detects_ssl_cert_verification_error():
    import ssl
    assert _is_permanent_tls_failure(
        ssl.SSLCertVerificationError('certificate verify failed')) is True


def test_is_permanent_tls_failure_string_fallback_case_insensitive():
    assert _is_permanent_tls_failure(RuntimeError('Certificate Has Expired')) is True
    assert _is_permanent_tls_failure(
        RuntimeError('[SSL: CERTIFICATE_VERIFY_FAILED] whatever')) is True


def test_is_permanent_tls_failure_false_for_unrelated_errors():
    assert _is_permanent_tls_failure(RuntimeError('connection refused')) is False
    assert _is_permanent_tls_failure(OSError('timed out')) is False


def test_ordinary_connection_refused_still_retries_with_backoff():
    """A transient failure (unrelated to certs) must keep its existing
    retry-with-backoff behavior — this fix only fast-fails permanent TLS
    errors, nothing else."""
    attempts = {'n': 0}

    def factory(url, verify_tls, timeout, tls_server_name=None):
        attempts['n'] += 1
        raise RuntimeError('connection refused (simulated)')

    client = TrueNASWSClient('truenas.example', 443, transport_factory=factory,
                              sleep_fn=lambda s: None)
    with pytest.raises(TrueNASConnectionError):
        client._connect_with_backoff(max_attempts=4)
    assert attempts['n'] == 4


def test_lazy_connect_never_touches_network_at_construction():
    # No transport_factory network call should happen just by building the
    # client — proves "no crash on import / construction without network".
    called = {'n': 0}

    def factory(url, verify_tls, timeout, tls_server_name=None):
        called['n'] += 1
        raise AssertionError('should not connect until call()/connect() is invoked')

    TrueNASWSClient('truenas.example', 443, transport_factory=factory)
    assert called['n'] == 0


# ---------------------------------------------------------------------------
# Unexpected disconnect fails in-flight calls
# ---------------------------------------------------------------------------

def test_connect_raises_connection_error_directly_without_retry():
    calls = {'n': 0}

    def factory(url, verify_tls, timeout, tls_server_name=None):
        calls['n'] += 1
        raise OSError('refused')

    client = TrueNASWSClient('truenas.example', 443, transport_factory=factory)
    with pytest.raises(TrueNASConnectionError):
        client.connect()
    assert calls['n'] == 1
    assert not client.is_connected


def test_close_marks_disconnected_and_closes_transport():
    ft = FakeTransport()
    client = _client_with(ft)
    client.connect()
    assert client.is_connected
    ft.push({'jsonrpc': '2.0', 'method': 'noop'})  # unblock reader before close races it
    client.close()
    assert not client.is_connected
    assert ft.closed


def test_subscribe_registers_callback_and_dispatches_notifications():
    ft = FakeTransport()
    client = _client_with(ft)
    received = []

    def on_event(msg):
        received.append(msg)

    def do_subscribe():
        client.subscribe('core.get_jobs', callback=on_event)

    t = threading.Thread(target=do_subscribe)
    t.start()
    assert _wait_for(lambda: ft.sent)
    req = json.loads(ft.sent[0])
    assert req['method'] == 'core.subscribe'
    assert req['params'] == ['core.get_jobs']
    ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': True})
    t.join(timeout=2)

    # A later unsolicited notification (no 'id') for that name is dispatched.
    ft.push({'method': 'core.get_jobs', 'params': {'id': 42}})
    assert _wait_for(lambda: received)
    assert received[0]['params'] == {'id': 42}
    _shutdown(client, ft)


def test_unsubscribe_stops_further_dispatch():
    ft = FakeTransport()
    client = _client_with(ft)
    received = []
    with client._subscriptions_lock:
        client._subscriptions['core.get_jobs'] = [received.append]
    client.unsubscribe('core.get_jobs', received.append)
    with client._subscriptions_lock:
        assert client._subscriptions.get('core.get_jobs') == []


def test_background_reconnect_recovers_after_unexpected_disconnect():
    ft1 = FakeTransport()
    ft2 = FakeTransport()
    transports = iter([ft1, ft2])

    def factory(url, verify_tls, timeout, tls_server_name=None):
        return next(transports)

    client = TrueNASWSClient('truenas.example', 443, transport_factory=factory,
                              sleep_fn=lambda s: None)
    client.connect()
    assert client.is_connected

    ft1.push_error(ConnectionResetError('simulated socket drop'))
    # is_connected alone is a bad wait condition here: it's already True
    # before the drop is even processed. Wait for the *new* transport to
    # actually take over instead.
    assert _wait_for(lambda: client._ws is ft2, timeout=3)
    assert client.is_connected
    _shutdown(client, ft2)


def test_reconnect_relogins_with_stored_api_key():
    ft1 = FakeTransport()
    ft2 = FakeTransport()
    transports = iter([ft1, ft2])

    def factory(url, verify_tls, timeout, tls_server_name=None):
        return next(transports)

    client = TrueNASWSClient('truenas.example', 443, transport_factory=factory,
                              sleep_fn=lambda s: None)
    client.connect()

    def do_login():
        client.login('ro-key-123')

    t = threading.Thread(target=do_login)
    t.start()
    assert _wait_for(lambda: ft1.sent)
    req = json.loads(ft1.sent[0])
    ft1.push({'jsonrpc': '2.0', 'id': req['id'], 'result': True})
    t.join(timeout=2)

    ft1.push_error(ConnectionResetError('drop'))
    assert _wait_for(lambda: ft2.sent, timeout=3)
    relogin_req = json.loads(ft2.sent[0])
    assert relogin_req['method'] == 'auth.login_with_api_key'
    assert relogin_req['params'] == ['ro-key-123']
    ft2.push({'jsonrpc': '2.0', 'id': relogin_req['id'], 'result': True})
    _shutdown(client, ft2)


def test_unexpected_disconnect_fails_pending_call():
    ft = FakeTransport()
    client = _client_with(ft, auto_reconnect=False)
    result_box = {}

    def do_call():
        try:
            client.call('system.info', timeout=2)
        except TrueNASConnectionError as e:
            result_box['exc'] = e

    t = threading.Thread(target=do_call)
    t.start()
    assert _wait_for(lambda: ft.sent)
    ft.push_error(ConnectionResetError('simulated socket drop'))
    t.join(timeout=2)
    assert isinstance(result_box.get('exc'), TrueNASConnectionError)
    assert not client.is_connected


# ---------------------------------------------------------------------------
# Regression: reader thread must survive malformed/unexpected frame shapes
# (code-reviewer + silent-failure-hunter finding #1)
# ---------------------------------------------------------------------------

def test_reader_survives_notification_with_list_params_without_method():
    """A dict frame with no 'id', no 'method', and 'params' as a LIST (not a
    dict) used to raise AttributeError inside _dispatch_notification
    (``(msg.get('params') or {}).get('msg')`` on a list), silently killing
    the reader thread while ``is_connected`` stayed True forever."""
    ft = FakeTransport()
    client = _client_with(ft)
    client.connect()

    ft.push({'params': [1, 2, 3]})  # no 'id', no 'method' -> notification path

    # The reader must still be alive: a subsequent call() must complete
    # normally rather than hang until a generic timeout.
    result_box = {}

    def do_call():
        result_box['result'] = client.call('system.info')

    t = threading.Thread(target=do_call)
    t.start()
    assert _wait_for(lambda: len(ft.sent) >= 1)
    req = json.loads(ft.sent[-1])
    ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': 'still-alive'})
    t.join(timeout=2)
    assert result_box['result'] == 'still-alive'
    assert client.is_connected
    _shutdown(client, ft)


def test_reader_survives_non_dict_frame():
    """A frame that's valid JSON but not an object at all (e.g. a bare
    list) must be discarded, not crash the reader."""
    ft = FakeTransport()
    client = _client_with(ft)
    client.connect()

    ft.push([1, 2, 3])

    result_box = {}

    def do_call():
        result_box['result'] = client.call('system.info')

    t = threading.Thread(target=do_call)
    t.start()
    assert _wait_for(lambda: len(ft.sent) >= 1)
    req = json.loads(ft.sent[-1])
    ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': 'still-alive'})
    t.join(timeout=2)
    assert result_box['result'] == 'still-alive'
    assert client.is_connected
    _shutdown(client, ft)


def test_reader_survives_arbitrary_exception_in_frame_handling(monkeypatch):
    """Belt-and-suspenders: even if some future frame shape trips an
    exception _handle_raw_frame itself doesn't anticipate, the read loop's
    outer try/except must still catch it, fail pending calls, and hand off
    to auto-reconnect instead of dying silently."""
    ft1 = FakeTransport()
    ft2 = FakeTransport()
    transports = iter([ft1, ft2])

    def factory(url, verify_tls, timeout, tls_server_name=None):
        return next(transports)

    client = TrueNASWSClient('truenas.example', 443, transport_factory=factory,
                              sleep_fn=lambda s: None)
    client.connect()

    def boom(raw):
        raise RuntimeError('unexpected frame shape')

    monkeypatch.setattr(client, '_handle_raw_frame', boom)
    ft1.push({'anything': 'at-all'})

    assert _wait_for(lambda: client._ws is ft2, timeout=3)
    assert client.is_connected
    _shutdown(client, ft2)


def test_protocol_error_with_null_id_is_logged_and_reader_survives(caplog):
    """A JSON-RPC error with id: null can't be matched to any pending
    call() — it used to fall into the notification path and vanish
    silently, leaving the real caller staring at a generic timeout."""
    ft = FakeTransport()
    client = _client_with(ft)
    client.connect()

    with caplog.at_level(logging.WARNING, logger='plugin.truenas.ws_client'):
        ft.push({'jsonrpc': '2.0', 'id': None, 'error': {'message': 'protocol failure'}})
        assert _wait_for(
            lambda: any('protocol-level error' in r.message for r in caplog.records))

    # And the reader must still be usable afterward.
    result_box = {}

    def do_call():
        result_box['result'] = client.call('system.info')

    t = threading.Thread(target=do_call)
    t.start()
    assert _wait_for(lambda: len(ft.sent) >= 1)
    req = json.loads(ft.sent[-1])
    ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': 'still-alive'})
    t.join(timeout=2)
    assert result_box['result'] == 'still-alive'
    _shutdown(client, ft)


# ---------------------------------------------------------------------------
# Regression: relogin failure after reconnect must not leave a half-alive
# connection reporting is_connected=True (finding #2)
# ---------------------------------------------------------------------------

def test_relogin_transient_failure_tears_down_and_retries_bounded():
    """A transient relogin failure (e.g. a timeout) must tear down the
    half-authenticated socket (never leaving is_connected=True with no
    valid session) and retry the full connect+relogin cycle, bounded by
    max_reconnect_attempts — not loop forever, and not silently give up
    after one failure leaving the client permanently disconnected without
    even exhausting its retry budget."""
    transports = []

    def factory(url, verify_tls, timeout, tls_server_name=None):
        ft = FakeTransport()
        transports.append(ft)
        return ft

    client = TrueNASWSClient('truenas.example', 443, transport_factory=factory,
                              sleep_fn=lambda s: None, timeout=0.05,
                              max_reconnect_attempts=3)
    client.connect()
    ft1 = transports[0]

    # Establish a session so relogin has an api_key to retry with.
    def do_login():
        client.login('ro-key')

    t = threading.Thread(target=do_login)
    t.start()
    assert _wait_for(lambda: ft1.sent)
    req = json.loads(ft1.sent[0])
    ft1.push({'jsonrpc': '2.0', 'id': req['id'], 'result': True})
    t.join(timeout=2)

    # Drop the connection — every subsequent reconnect's relogin call will
    # simply never get answered, forcing a timeout (transient failure) each
    # cycle, up to max_reconnect_attempts.
    ft1.push_error(ConnectionResetError('drop'))

    # 1 (initial) + max_reconnect_attempts reconnect sockets get created.
    assert _wait_for(lambda: len(transports) >= 1 + client.max_reconnect_attempts, timeout=3)
    # Give the background worker a moment to finish its last cycle and
    # release the reconnect guard.
    assert _wait_for(lambda: client._reconnecting is False, timeout=3)

    assert not client.is_connected
    assert client.last_error
    assert not client.needs_auth  # transient, not an auth rejection


def test_relogin_auth_rejection_sets_needs_auth_and_stops_retrying():
    """An auth rejection (bad/revoked key) must set needs_auth and stop
    retrying immediately — hammering a revoked key with more login attempts
    achieves nothing but audit-log spam against the appliance."""
    transports = []

    def factory(url, verify_tls, timeout, tls_server_name=None):
        ft = FakeTransport()
        transports.append(ft)
        return ft

    client = TrueNASWSClient('truenas.example', 443, transport_factory=factory,
                              sleep_fn=lambda s: None, max_reconnect_attempts=5)
    client.connect()
    ft1 = transports[0]

    def do_login():
        client.login('ro-key')

    t = threading.Thread(target=do_login)
    t.start()
    assert _wait_for(lambda: ft1.sent)
    req = json.loads(ft1.sent[0])
    ft1.push({'jsonrpc': '2.0', 'id': req['id'], 'result': True})
    t.join(timeout=2)

    ft1.push_error(ConnectionResetError('drop'))
    assert _wait_for(lambda: len(transports) >= 2, timeout=3)
    ft2 = transports[1]
    assert _wait_for(lambda: ft2.sent, timeout=3)
    relogin_req = json.loads(ft2.sent[-1])
    ft2.push({'jsonrpc': '2.0', 'id': relogin_req['id'],
              'error': {'message': 'invalid api key'}})

    assert _wait_for(lambda: client.needs_auth, timeout=3)
    assert _wait_for(lambda: client._reconnecting is False, timeout=3)
    # Only ONE reconnect socket was opened for the relogin attempt — no
    # retry hammer against the now-known-bad key.
    assert len(transports) == 2
    assert not client.is_connected


# ---------------------------------------------------------------------------
# Regression: close() must cancel an in-flight background reconnect instead
# of letting it resurrect the connection with a stale api_key (finding #3)
# ---------------------------------------------------------------------------

def test_close_during_backoff_sleep_cancels_pending_reconnect():
    """Simulates the exact race from the report: the socket drops, the
    background worker enters its backoff sleep, and close() is called
    (e.g. because the operator just rotated the API key and saved a new
    config) WHILE the worker is asleep. When it wakes, it must NOT open a
    new socket and relogin with the old key — it must notice _closed and
    give up."""
    ft1 = FakeTransport()
    later_transports = []

    def factory(url, verify_tls, timeout, tls_server_name=None):
        # First call (the initial explicit connect()) hands out ft1.
        # Every later call (a reconnect attempt) would hand out a fresh
        # transport — but none should ever be requested after close().
        ft = FakeTransport()
        later_transports.append(ft)
        return ft

    client = TrueNASWSClient('truenas.example', 443,
                              transport_factory=lambda *a, **k: ft1,
                              sleep_fn=lambda s: None, max_reconnect_attempts=1)
    client.connect()

    # Force connect() to fail for every RECONNECT attempt (simulating the
    # appliance being briefly unreachable), so _connect_with_backoff has to
    # go through its backoff-sleep path where we'll race the close().
    def failing_factory(url, verify_tls, timeout, tls_server_name=None):
        raise OSError('simulated: still unreachable')

    client._transport_factory = failing_factory

    # sleep_fn doubles as the injection point for the race: the very moment
    # the reconnect worker sleeps between attempts, close() fires.
    def sleep_and_close(_delay):
        client.close()

    client._sleep = sleep_and_close
    # Use enough attempts that at least one backoff sleep happens.
    client.max_reconnect_attempts = 3

    ft1.push_error(ConnectionResetError('drop'))
    # Wait for the one state change that can only happen after close() ran:
    # _closed flips False->True exactly once, monotonically — a safe
    # condition to poll for, unlike _reconnecting (which is False both
    # before recovery starts AND after it ends, including within the same
    # instant on a fake, sleep-free transport).
    assert _wait_for(lambda: client._closed, timeout=3)
    assert _wait_for(lambda: not client._reconnecting, timeout=3)
    assert not client.is_connected
    # The stale api_key was never used again over a resurrected socket:
    # login() was never called from this test, so _api_key is None and
    # there is nothing to leak — the key assertion is that connect() did
    # NOT quietly succeed and flip is_connected back to True.


class _RaceLock:
    """Wraps a real lock; the FIRST acquire() triggers a one-shot callback
    BEFORE actually acquiring — used to force a concurrent close() to land
    in the exact gap a non-atomic "is it closed?" check would have left
    open, proving the real gate (inside connect(), under this same lock)
    closes it instead."""

    def __init__(self, real_lock, on_first_acquire):
        self._real = real_lock
        self._on_first_acquire = on_first_acquire
        self._triggered = False

    def acquire(self, *a, **kw):
        if not self._triggered:
            self._triggered = True
            self._on_first_acquire()
        return self._real.acquire(*a, **kw)

    def release(self):
        return self._real.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc_info):
        self.release()


def test_toctou_close_racing_the_lock_acquisition_does_not_resurrect_stale_key():
    """The residual TOCTOU from the previous round: connect() used to set
    ``self._closed = False`` unconditionally, and callers checked
    ``_closed`` BEFORE calling connect() — a close() landing in the gap
    between that check and connect() acquiring ``_connect_lock`` would
    still let the reconnect worker open a fresh socket and relogin with
    the stale api_key. Force that exact interleave by wrapping
    ``_connect_lock`` so the first acquire (by the reconnect worker's
    ``connect()`` call) triggers a REAL close() from another thread first,
    then proceeds — proving the post-acquisition, atomic recheck inside
    ``connect()`` refuses to resurrect instead of racing through."""
    ft1 = FakeTransport()
    later_transports = []

    def factory(url, verify_tls, timeout, tls_server_name=None):
        ft = FakeTransport()
        later_transports.append(ft)
        return ft

    client = TrueNASWSClient('truenas.example', 443,
                              transport_factory=lambda *a, **k: ft1,
                              sleep_fn=lambda s: None, max_reconnect_attempts=2)
    client.connect()

    # Establish a session — a resurrection would show up as a relogin
    # attempt on a NEW transport carrying this same stale key.
    def do_login():
        client.login('stale-key')

    t = threading.Thread(target=do_login)
    t.start()
    assert _wait_for(lambda: ft1.sent)
    req = json.loads(ft1.sent[0])
    ft1.push({'jsonrpc': '2.0', 'id': req['id'], 'result': True})
    t.join(timeout=2)

    # From here on, any reconnect attempt would use `factory` (a fresh
    # transport each time) — but the race below must prevent one from ever
    # being requested at all.
    client._transport_factory = factory

    def close_from_another_thread():
        close_thread = threading.Thread(target=client.close)
        close_thread.start()
        close_thread.join(timeout=2)

    # Wrap the SAME lock object connect()/close()/_teardown_socket all
    # reference via self._connect_lock — the first time anything acquires
    # it after the drop, a real close() runs to completion first.
    client._connect_lock = _RaceLock(client._connect_lock, close_from_another_thread)

    ft1.push_error(ConnectionResetError('simulated socket drop'))

    assert _wait_for(lambda: client._closed, timeout=3)
    assert _wait_for(lambda: not client._reconnecting, timeout=3)

    assert not client.is_connected
    # The core assertion: connect() must have refused to open ANY new
    # transport once it observed (atomically, post-race) that the client
    # was closed — so no reconnect socket, and no relogin with the stale
    # key, ever happened.
    assert later_transports == []


def test_explicit_connect_reopens_after_close():
    """A deliberate connect() call after close() must succeed and clear
    _closed — close() is not a permanent brick, only a cancellation of
    automatic recovery."""
    ft = FakeTransport()
    client = _client_with(ft)
    client.connect()
    ft.push({'jsonrpc': '2.0', 'method': 'noop'})
    client.close()
    assert client._closed

    ft2 = FakeTransport()
    client._transport_factory = lambda *a, **k: ft2
    client.connect()
    assert client.is_connected
    assert not client._closed
    _shutdown(client, ft2)


# ---------------------------------------------------------------------------
# Regression: concurrent drops must not spawn duplicate reconnect workers
# (finding #5)
# ---------------------------------------------------------------------------

def test_concurrent_disconnect_does_not_spawn_duplicate_worker(monkeypatch):
    """Deterministic version of the race: rather than relying on real thread
    timing (a second disconnect racing to land while the first recovery
    thread is still mid-flight — inherently flaky to reproduce), simulate
    "a recovery is already in progress" directly by pre-setting the guard
    flag, then assert a second disconnect does not spawn another worker.
    A third disconnect, once the guard is cleared (recovery finished), must
    be free to spawn a new one."""
    ft = FakeTransport()
    client = _client_with(ft)
    client.connect()

    spawned = []

    class DummyThread:
        def __init__(self, target=None, name=None, daemon=None):
            spawned.append(name)

        def start(self):
            pass  # never actually run _background_reconnect in this test

    monkeypatch.setattr(threading, 'Thread', DummyThread)

    # Simulate: a recovery worker is already active.
    client._reconnecting = True
    client._handle_unexpected_disconnect('drop while recovery in progress')
    assert spawned == []  # guard prevented a duplicate spawn

    # Once that (simulated) recovery finishes and clears the guard, a new
    # drop is free to start a fresh worker.
    client._reconnecting = False
    client._handle_unexpected_disconnect('drop after recovery finished')
    assert len(spawned) == 1
    assert spawned[0].startswith('truenas-reconnect-')


# ---------------------------------------------------------------------------
# Regression: the transport must not churn reconnect/relogin every
# ``timeout`` seconds on a perfectly idle, healthy connection (finding #6)
# ---------------------------------------------------------------------------

def test_default_transport_factory_disables_recv_timeout(monkeypatch):
    calls = {}

    class _FakeRealWS:
        def settimeout(self, value):
            calls['settimeout'] = value

    fake_websocket_module = types.SimpleNamespace(
        create_connection=lambda url, timeout=None, sslopt=None: _FakeRealWS())
    monkeypatch.setitem(sys.modules, 'websocket', fake_websocket_module)

    from core.ws_client import _default_transport_factory
    ws = _default_transport_factory('wss://truenas.example:443/api/current', False, 10.0)

    assert calls['settimeout'] is None
    assert isinstance(ws, _FakeRealWS)


# ---------------------------------------------------------------------------
# Regression: real TrueNAS instances are commonly reached by LAN IP but
# present a CA-issued cert bound to a DNS name (confirmed live 2026-07-20
# against .64: cert CN=nas-remote.example.com, dialed via IP 192.0.2.64) —
# verify_tls=True must not fail with "IP address mismatch" when the caller
# supplies the correct SNI/verification name separately from the dial host.
# ---------------------------------------------------------------------------

def test_default_transport_factory_passes_server_hostname_for_sni(monkeypatch):
    captured = {}

    class _FakeRealWS:
        def settimeout(self, value):
            pass

    def fake_create_connection(url, timeout=None, sslopt=None):
        captured['sslopt'] = sslopt
        return _FakeRealWS()

    fake_websocket_module = types.SimpleNamespace(create_connection=fake_create_connection)
    monkeypatch.setitem(sys.modules, 'websocket', fake_websocket_module)

    from core.ws_client import _default_transport_factory
    _default_transport_factory(
        'wss://192.0.2.64:444/api/current', True, 10.0, tls_server_name='nas-remote.example.com')

    assert captured['sslopt'] == {'server_hostname': 'nas-remote.example.com'}


def test_default_transport_factory_no_server_hostname_verifies_against_url_host(monkeypatch):
    captured = {}

    class _FakeRealWS:
        def settimeout(self, value):
            pass

    def fake_create_connection(url, timeout=None, sslopt=None):
        captured['sslopt'] = sslopt
        return _FakeRealWS()

    fake_websocket_module = types.SimpleNamespace(create_connection=fake_create_connection)
    monkeypatch.setitem(sys.modules, 'websocket', fake_websocket_module)

    from core.ws_client import _default_transport_factory
    _default_transport_factory('wss://truenas.example:443/websocket', True, 10.0)

    # No override supplied -> let websocket-client verify against url's own
    # host, same as before this feature existed.
    assert captured['sslopt'] is None


def test_client_threads_tls_server_name_through_to_transport_factory():
    captured = {}
    ft = FakeTransport()

    def fake_factory(url, verify_tls, timeout, tls_server_name=None):
        captured['tls_server_name'] = tls_server_name
        return ft

    client = TrueNASWSClient(
        host='192.0.2.64', port=444, use_tls=True, verify_tls=True,
        tls_server_name='nas-remote.example.com', transport_factory=fake_factory)
    client.connect()

    assert captured['tls_server_name'] == 'nas-remote.example.com'
    _shutdown(client, ft)


# ---------------------------------------------------------------------------
# is_authenticated — F1's subsystem routes gate their first call per
# request on this, so it must track the CURRENT socket's session, not just
# whether an api_key was ever remembered.
# ---------------------------------------------------------------------------

def test_is_authenticated_false_before_any_login():
    ft = FakeTransport()
    client = _client_with(ft)
    client.connect()
    assert client.is_authenticated is False
    _shutdown(client, ft)


def test_is_authenticated_true_after_successful_login():
    ft = FakeTransport()
    client = _client_with(ft)

    def do_login():
        client.login('ro-key')

    t = threading.Thread(target=do_login)
    t.start()
    assert _wait_for(lambda: ft.sent)
    req = json.loads(ft.sent[0])
    ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': True})
    t.join(timeout=2)

    assert client.is_authenticated is True
    _shutdown(client, ft)


def test_is_authenticated_false_after_close():
    ft = FakeTransport()
    client = _client_with(ft)

    def do_login():
        client.login('ro-key')

    t = threading.Thread(target=do_login)
    t.start()
    assert _wait_for(lambda: ft.sent)
    req = json.loads(ft.sent[0])
    ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': True})
    t.join(timeout=2)
    assert client.is_authenticated is True

    ft.push({'jsonrpc': '2.0', 'method': 'noop'})
    client.close()
    assert client.is_authenticated is False


def test_is_authenticated_false_after_unexpected_disconnect_before_relogin():
    """A dropped socket must never report a session that's actually gone —
    even before the background worker has had a chance to relogin."""
    ft = FakeTransport()
    client = _client_with(ft, auto_reconnect=False)

    def do_login():
        client.login('ro-key')

    t = threading.Thread(target=do_login)
    t.start()
    assert _wait_for(lambda: ft.sent)
    req = json.loads(ft.sent[0])
    ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': True})
    t.join(timeout=2)
    assert client.is_authenticated is True

    ft.push_error(ConnectionResetError('drop'))
    assert _wait_for(lambda: not client.is_connected)
    assert client.is_authenticated is False


def test_is_authenticated_true_again_after_successful_relogin_on_reconnect():
    ft1 = FakeTransport()
    ft2 = FakeTransport()
    transports = iter([ft1, ft2])

    def factory(url, verify_tls, timeout, tls_server_name=None):
        return next(transports)

    client = TrueNASWSClient('truenas.example', 443, transport_factory=factory,
                              sleep_fn=lambda s: None)
    client.connect()

    def do_login():
        client.login('ro-key')

    t = threading.Thread(target=do_login)
    t.start()
    assert _wait_for(lambda: ft1.sent)
    req = json.loads(ft1.sent[0])
    ft1.push({'jsonrpc': '2.0', 'id': req['id'], 'result': True})
    t.join(timeout=2)

    ft1.push_error(ConnectionResetError('drop'))
    assert _wait_for(lambda: ft2.sent, timeout=3)
    relogin_req = json.loads(ft2.sent[-1])
    ft2.push({'jsonrpc': '2.0', 'id': relogin_req['id'], 'result': True})

    assert _wait_for(lambda: client.is_authenticated, timeout=3)
    _shutdown(client, ft2)


# ---------------------------------------------------------------------------
# Regression: a socket drop landing between the login response arriving and
# _authenticated being set must not leave is_authenticated=True over a dead
# socket (code-reviewer + silent-failure-hunter, same finding independently).
# ---------------------------------------------------------------------------

def test_login_race_disconnect_between_response_and_authenticated_flag():
    """Forces the exact interleave: the drop is injected right after
    call('auth.login_with_api_key', ...) returns its result but BEFORE
    _do_login's atomic assignment runs — by wrapping client.call itself,
    the one seam _do_login actually goes through."""
    ft = FakeTransport()
    client = _client_with(ft, auto_reconnect=False)
    client.connect()

    original_call = client.call

    def racy_call(method, params=None, timeout=None):
        result = original_call(method, params=params, timeout=timeout)
        if method == 'auth.login_with_api_key':
            # Simulate the reader thread noticing a drop in the exact
            # window between the response arriving and _do_login's own
            # "still connected?" check + assignment.
            client._handle_unexpected_disconnect('simulated drop during login')
        return result

    client.call = racy_call

    result_box = {}

    def do_login():
        try:
            client.login('ro-key')
        except TrueNASConnectionError as e:
            result_box['exc'] = e

    t = threading.Thread(target=do_login)
    t.start()
    assert _wait_for(lambda: ft.sent)
    req = json.loads(ft.sent[0])
    ft.push({'jsonrpc': '2.0', 'id': req['id'], 'result': True})
    t.join(timeout=2)

    assert isinstance(result_box.get('exc'), TrueNASConnectionError)
    # The core assertion: is_authenticated must NEVER report True over what
    # is now a disconnected socket, regardless of the login response having
    # said "yes" a moment earlier.
    assert client.is_authenticated is False
    assert not client.is_connected
