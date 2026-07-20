# -*- coding: utf-8 -*-
"""Shared test doubles for subsystem unit tests — not a test module itself
(no ``test_*`` functions), just a duck-typed stand-in for
``TrueNASWSClient`` so subsystem modules can be tested without any real
socket, mirroring the FakeTransport pattern in test_ws_client.py but at the
``call()`` level instead of the raw wire level.
"""


class FakeConn:
    """Canned JSON-RPC responses keyed by method name; records every call
    made (method, params) for assertions. Raises loudly (AssertionError) on
    an unexpected method — a subsystem calling something the test didn't
    anticipate should fail the test, not silently return None."""

    def __init__(self, responses=None, is_authenticated=True, needs_auth=False):
        self.responses = dict(responses or {})
        self.calls = []
        # Route-level tests (routes/api.py's _get_authenticated_connection)
        # also duck-type against `is_authenticated`/`needs_auth`/`login` —
        # harmless extras for plain subsystem-level tests that never touch
        # them.
        self.is_authenticated = is_authenticated
        self.needs_auth = needs_auth
        self.login_calls = []
        self.timeouts = []  # per-call timeout kwarg, parallel to self.calls

    def call(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        self.timeouts.append(timeout)
        if method not in self.responses:
            raise AssertionError(f'FakeConn: no canned response configured for {method!r}')
        value = self.responses[method]
        if isinstance(value, BaseException):
            raise value
        return value

    def login(self, api_key):
        self.login_calls.append(api_key)
        self.is_authenticated = True

    def methods_called(self):
        return [c[0] for c in self.calls]
