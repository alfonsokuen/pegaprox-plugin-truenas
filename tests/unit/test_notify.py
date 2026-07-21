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


def test_send_whatsapp_skips_cleanly_when_not_fully_configured():
    ok, err = notify.send_whatsapp('https://evolution.example', None, 'key', 'target', [])
    assert ok is False
    assert 'not fully configured' in err


def test_send_whatsapp_posts_to_the_evolution_sendtext_endpoint(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured['url'] = req.full_url
        captured['apikey'] = req.get_header('Apikey')
        captured['body'] = req.data
        return _FakeResponse(200)

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    ok, err = notify.send_whatsapp(
        'https://evolution01.idkmanager.com', 'cum', 'the-key', '593999999999',
        [{'message': 'pool tank at 91%'}])
    assert ok is True
    assert err is None
    assert captured['url'] == 'https://evolution01.idkmanager.com/message/sendText/cum'
    assert captured['apikey'] == 'the-key'
    assert b'593999999999' in captured['body']
    assert b'pool tank at 91' in captured['body']


def test_send_whatsapp_http_error_never_raises(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, 'bad key', {}, None)

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    ok, err = notify.send_whatsapp('https://evolution.example', 'cum', 'bad', 'target', [{'message': 'x'}])
    assert ok is False
    assert '401' in err
