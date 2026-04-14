# linkedin/webhooks.py
"""Webhook delivery for external service notifications."""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request

from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

from linkedin.conf import WEBHOOK_SECRET, WEBHOOK_URL

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10
_ALLOWED_SCHEMES = ("http", "https")


def _validate_webhook_target(url: str) -> None:
    """Reject schemes we don't speak and hostnames that resolve to private ranges.

    Called at send time (not import time) because WEBHOOK_URL is user-supplied
    config and we treat delivery as best-effort. An invalid URL logs and returns
    — we do not raise into the caller's task handler.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"webhook scheme must be http/https, got {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("webhook URL has no hostname")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"webhook host {host!r} does not resolve: {exc}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast:
            raise ValueError(f"webhook host {host!r} resolves to a non-public address {addr}")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
def _post(url: str, payload: bytes, headers: dict):
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
        return resp.status


def fire_webhook(event_type: str, data: dict):
    """POST a webhook event to WEBHOOK_URL. Retries 3x with backoff."""
    if not WEBHOOK_URL:
        logger.info("fire_webhook skipped — WEBHOOK_URL not set (event=%s)", event_type)
        return

    try:
        _validate_webhook_target(WEBHOOK_URL)
    except ValueError:
        logger.warning("Webhook %s rejected: invalid WEBHOOK_URL", event_type, exc_info=True)
        return

    from django.utils import timezone

    payload = json.dumps({
        "event": event_type,
        "timestamp": timezone.now().isoformat(),
        "data": data,
    }).encode()

    headers = {"Content-Type": "application/json"}
    if WEBHOOK_SECRET:
        headers["X-Webhook-Secret"] = WEBHOOK_SECRET

    try:
        status = _post(WEBHOOK_URL, payload, headers)
        logger.info("Webhook %s delivered to %s (HTTP %s)", event_type, WEBHOOK_URL, status)
    except (urllib.error.URLError, TimeoutError, RetryError):
        logger.warning("Webhook %s delivery failed after retries", event_type, exc_info=True)
