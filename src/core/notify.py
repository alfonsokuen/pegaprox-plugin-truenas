# -*- coding: utf-8 -*-
"""Notification channels for the F4a alert engine (``core/alerts.py``).

stdlib ``urllib.request`` rather than adding a ``requests`` dependency for
a single JSON POST — CT119 has no internet access, and every dependency
this plugin adds has to be confirmed present (or vendored) offline first;
a webhook POST doesn't need more than the stdlib already gives it.
"""

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger('plugin.truenas.notify')

DEFAULT_TIMEOUT_S = 10.0


def send_webhook(url, notifications, timeout=DEFAULT_TIMEOUT_S):
    """POST ``notifications`` (a list of the dicts ``alerts.evaluate()``
    returns) as JSON to ``url``. Never raises — mirrors ``core.subsystem
    .safe_call``'s philosophy: one failed delivery must not crash the
    poller loop or block the next cycle's other channels. Returns
    ``(ok, error_or_None)``."""
    if not url:
        return False, 'no webhook_url configured'
    body = json.dumps({'notifications': notifications}).encode('utf-8')
    req = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 300:
                return False, f'webhook returned HTTP {resp.status}'
        return True, None
    except urllib.error.HTTPError as e:
        return False, f'webhook returned HTTP {e.code}'
    except Exception as e:
        # DNS failure, connection refused, timeout, TLS error — all land
        # here. Logged by the caller (poller.py), not here, so a single
        # log line names both the channel and which poll cycle it was.
        return False, str(e)
