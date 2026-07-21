# -*- coding: utf-8 -*-
"""data_protection subsystem: cloudsync/rsync/certificate posture (F6).

The tests here exist primarily to lock down a real security finding
(live-confirmed against .64): cloudsync.query's raw record carries the
cloud provider's SECRET KEY in cleartext, and certificate.query's raw
record carries the certificate's PRIVATE KEY in cleartext. Every test
below that constructs a "realistic" fixture includes those secret fields
on purpose, so a regression that starts passing them through would be
caught here, not discovered live in production."""

from subsystems import data_protection
from tests.unit.fakes import FakeConn

# Every "PEM"/secret-shaped string below is a SYNTHETIC placeholder (a
# realistic prefix + literal "...") built to look like the real fields
# TrueNAS returns — none of it is real key/credential material. The whole
# point of this file is to prove those shapes get stripped before this
# subsystem's output ever leaves the plugin (see data_protection.py's
# module docstring for the live-confirmed leak this guards against).


def _cloudsync_row(id_=3, secret_key='K0059LFFat+0tIIh1LpjWw+KW2ffRBM'):
    return {
        'id': id_, 'description': 'nextcloud-idkmanager', 'path': '/mnt/x/nextcloud',
        'enabled': True, 'direction': 'PUSH',
        'schedule': {'minute': '0', 'hour': '2', 'dom': '*', 'month': '*', 'dow': 'wed'},
        'credentials': {
            'id': 2, 'name': 'Backblaze B2',
            'provider': {'type': 'B2', 'account': '0055914d58207b4000000000d', 'key': secret_key},
        },
        'job': {
            'id': 556511, 'state': 'SUCCESS', 'time_started': {'$date': 1}, 'time_finished': {'$date': 2},
            'error': None, 'progress': {'percent': 100},
            'logs_excerpt': 'x' * 5000, 'logs_path': '/var/log/jobs/556511.log',
            'method': 'cloudsync.sync', 'arguments': [3],
        },
    }


def _cert_row(id_=4, private_key='-----BEGIN PRIVATE KEY-----\nMIIEvQIBADAN...'):
    return {
        'id': id_, 'name': 'server_open_vpn', 'common': 'vpn.example.com',
        'san': ['vpn.example.com'], 'cert_type': 'CERTIFICATE', 'key_type': 'RSA',
        'until': 'Thu Jan 23 00:50:50 2025', 'expired': True, 'renew_days': 10,
        'add_to_trusted_store': False,
        'certificate': '-----BEGIN CERTIFICATE-----\nMIIEzDCCA7Sg...',
        'privatekey': private_key,
        'CSR': None, 'chain': '-----BEGIN CERTIFICATE-----\nMIIEzDCCA7Sg...',
        'chain_list': ['-----BEGIN CERTIFICATE-----\nMIIEzDCCA7Sg...'],
    }


def test_cloudsync_tasks_never_leaks_the_provider_secret_key():
    conn = FakeConn({'cloudsync.query': [_cloudsync_row()]})
    result = data_protection.cloudsync_tasks(conn)
    assert len(result) == 1
    assert 'credentials' not in result[0]
    dumped = str(result[0])
    assert 'K0059LFFat+0tIIh1LpjWw+KW2ffRBM' not in dumped


def test_cloudsync_tasks_keeps_safe_metadata():
    conn = FakeConn({'cloudsync.query': [_cloudsync_row()]})
    result = data_protection.cloudsync_tasks(conn)
    assert result[0]['description'] == 'nextcloud-idkmanager'
    assert result[0]['enabled'] is True
    assert result[0]['schedule']['dow'] == 'wed'


def test_cloudsync_tasks_projects_job_without_the_giant_log_excerpt():
    conn = FakeConn({'cloudsync.query': [_cloudsync_row()]})
    result = data_protection.cloudsync_tasks(conn)
    job = result[0]['job']
    assert job['state'] == 'SUCCESS'
    assert 'logs_excerpt' not in job
    assert 'logs_path' not in job


def test_cloudsync_tasks_returns_empty_list_when_middleware_returns_none():
    conn = FakeConn({'cloudsync.query': None})
    assert data_protection.cloudsync_tasks(conn) == []


def test_certificates_never_leaks_the_private_key():
    conn = FakeConn({'certificate.query': [_cert_row()]})
    result = data_protection.certificates(conn)
    assert len(result) == 1
    assert 'privatekey' not in result[0]
    assert 'certificate' not in result[0]
    assert 'CSR' not in result[0]
    assert 'chain' not in result[0]
    assert 'chain_list' not in result[0]
    dumped = str(result[0])
    assert 'BEGIN PRIVATE KEY' not in dumped


def test_certificates_keeps_expiry_metadata():
    conn = FakeConn({'certificate.query': [_cert_row()]})
    result = data_protection.certificates(conn)
    assert result[0]['common'] == 'vpn.example.com'
    assert result[0]['expired'] is True
    assert result[0]['renew_days'] == 10


def test_rsync_tasks_never_leaks_credentials_even_defensively():
    row = {
        'id': 1, 'path': '/mnt/x', 'remotehost': 'backup.example.com', 'remoteport': 22,
        'direction': 'PUSH', 'enabled': True, 'schedule': {},
        'ssh_credentials': {'id': 1, 'attributes': {'password': 'hunter2'}},
        'job': None,
    }
    conn = FakeConn({'rsynctask.query': [row]})
    result = data_protection.rsync_tasks(conn)
    assert 'ssh_credentials' not in result[0]
    assert 'hunter2' not in str(result[0])


def test_list_isolates_a_failing_certificate_query_from_working_cloudsync():
    from core.errors import TrueNASConnectionError
    conn = FakeConn({
        'cloudsync.query': [_cloudsync_row()],
        'rsynctask.query': [],
        'certificate.query': TrueNASConnectionError('timed out'),
    })
    result = data_protection.data_protection.list(conn)
    assert len(result['cloudsync']) == 1
    assert result['certificates'] == []
    assert 'timed out' in result['certificates_error']
