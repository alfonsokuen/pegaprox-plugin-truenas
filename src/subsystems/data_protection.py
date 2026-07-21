# -*- coding: utf-8 -*-
"""Data protection posture (F6, read-only): cloudsync/rsync task status +
certificate expiry. Turns the Fleet/Overview from "space + alerts" into a
real risk tally an operator can act on without opening TrueNAS: "backup
hasn't run in 9 days", "cert expires in 12 days".

SECURITY — deliberate deviation from this codebase's usual attrs-
passthrough convention (see ``core/subsystem.py``'s module docstring):
confirmed LIVE against a real `.64` record that ``cloudsync.query``
returns the configured cloud provider's credential in cleartext —
``credentials.provider`` includes a real Backblaze B2 ``key`` (secret
application key), not just an id/name. ``certificate.query`` likewise
returns the certificate's PRIVATE KEY in cleartext (``privatekey``, full
PEM) alongside the public certificate/chain/CSR. Passing either through
as-is — like every other subsystem in this plugin does deliberately — would
leak a real secret to anyone with ``storage.view`` on the PegaProx panel,
not just admins. Every function below returns an explicit ALLOW-LIST of
fields; nothing here ever returns ``credentials``, ``privatekey``,
``certificate``, ``CSR``, ``chain``, or ``chain_list``.

``job`` sub-objects (cloudsync/rsync's last-run info) are projected the
same way, not embedded whole: a real job record carries ``logs_excerpt``
that can run to megabytes (confirmed live, F3's fleet.py hit the same
issue) — embedding it here would blow up this route's response size for
no benefit this feature needs (last state/time/error, not the full log).
"""

from core.subsystem import Subsystem, safe_call

_CLOUDSYNC_FIELDS = ('id', 'description', 'path', 'enabled', 'schedule', 'direction')
_RSYNC_FIELDS = ('id', 'path', 'remotehost', 'remoteport', 'direction', 'enabled', 'schedule')
_CERT_FIELDS = (
    'id', 'name', 'common', 'san', 'cert_type', 'key_type',
    'until', 'expired', 'renew_days', 'add_to_trusted_store',
)
_JOB_SUMMARY_FIELDS = ('id', 'state', 'time_started', 'time_finished', 'error', 'progress')


def _project_job(job):
    if not isinstance(job, dict):
        return None
    return {f: job.get(f) for f in _JOB_SUMMARY_FIELDS}


def _project(record, fields):
    out = {f: record.get(f) for f in fields}
    out['job'] = _project_job(record.get('job'))
    return out


def cloudsync_tasks(conn):
    """``cloudsync.query`` projected to safe fields only — see module
    docstring: the raw record carries the cloud provider's SECRET KEY in
    cleartext (``credentials.provider.key``), confirmed live."""
    return [_project(r, _CLOUDSYNC_FIELDS) for r in (conn.call('cloudsync.query') or [])]


def rsync_tasks(conn):
    """``rsynctask.query`` projected the same defensive way. No live
    record with inline credentials was observed (0 rows on `.64` at
    verification time) — the allow-list discipline still applies rather
    than assuming safety from an empty sample."""
    return [_project(r, _RSYNC_FIELDS) for r in (conn.call('rsynctask.query') or [])]


def certificates(conn):
    """``certificate.query`` projected to safe fields only — the raw
    record carries the PRIVATE KEY in cleartext (``privatekey``, full PEM),
    confirmed live, plus public cert/chain/CSR content this feature has no
    use for (it only tracks expiry)."""
    return [_project(r, _CERT_FIELDS) for r in (conn.call('certificate.query') or [])]


class DataProtectionSubsystem(Subsystem):
    SUBSYSTEM_ID = 'data_protection'

    def list(self, conn):
        """Each collection fetched independently via ``safe_call`` — a
        failing ``certificate.query`` must not also hide cloudsync/rsync
        task status, same isolation rule as every other multi-call
        subsystem in this plugin."""
        cloudsync, cloudsync_error = safe_call(
            'cloudsync.query', lambda: cloudsync_tasks(conn), [])
        rsync, rsync_error = safe_call(
            'rsynctask.query', lambda: rsync_tasks(conn), [])
        certs, certs_error = safe_call(
            'certificate.query', lambda: certificates(conn), [])
        return {
            'cloudsync': cloudsync, 'cloudsync_error': cloudsync_error,
            'rsync': rsync, 'rsync_error': rsync_error,
            'certificates': certs, 'certificates_error': certs_error,
        }


data_protection = DataProtectionSubsystem()
