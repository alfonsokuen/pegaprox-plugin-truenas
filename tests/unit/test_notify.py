# -*- coding: utf-8 -*-
"""core/notify.py: webhook delivery never raises, reports (ok, error)."""

import urllib.error

from core import notify


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_webhook_with_no_url_configured_is_not_an_error_just_skipped():
    ok, err = notify.send_webhook(None, [{'message': 'x'}])
    assert ok is False
    assert 'no webhook_url' in err


def test_send_webhook_success(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['body'] = req.data
        return _FakeResponse(200)

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    ok, err = notify.send_webhook('http://example.invalid/hook', [{'message': 'x'}])
    assert ok is True
    assert err is None
    assert captured['url'] == 'http://example.invalid/hook'
    assert b'"message": "x"' in captured['body']


def test_send_webhook_http_error_never_raises(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, 'boom', {}, None)

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    ok, err = notify.send_webhook('http://example.invalid/hook', [{'message': 'x'}])
    assert ok is False
    assert '500' in err


def test_send_webhook_connection_error_never_raises(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise OSError('connection refused')

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    ok, err = notify.send_webhook('http://example.invalid/hook', [{'message': 'x'}])
    assert ok is False
    assert 'connection refused' in err
