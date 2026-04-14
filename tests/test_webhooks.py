# tests/test_webhooks.py
"""Tests for linkedin/webhooks.py — SSRF validator and retry behavior."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from linkedin.webhooks import _validate_webhook_target, fire_webhook


class TestValidateWebhookTarget:
    def test_rejects_non_http_scheme(self):
        with pytest.raises(ValueError, match="scheme"):
            _validate_webhook_target("file:///etc/passwd")
        with pytest.raises(ValueError, match="scheme"):
            _validate_webhook_target("gopher://localhost/")

    def test_rejects_loopback_v4(self):
        with pytest.raises(ValueError, match="non-public"):
            _validate_webhook_target("http://127.0.0.1/hook")

    def test_rejects_loopback_v6(self):
        with pytest.raises(ValueError, match="non-public"):
            _validate_webhook_target("http://[::1]/hook")

    def test_rejects_rfc1918_range(self):
        with pytest.raises(ValueError, match="non-public"):
            _validate_webhook_target("http://10.0.0.5/hook")
        with pytest.raises(ValueError, match="non-public"):
            _validate_webhook_target("http://192.168.1.1/hook")

    def test_rejects_unresolvable_host(self):
        with pytest.raises(ValueError, match="resolve"):
            _validate_webhook_target(
                "http://this-host-definitely-does-not-exist-12345.invalid/",
            )

    def test_accepts_public_https(self):
        # Use a hostname that resolves to a public address. example.com's
        # DNS entries live in public space, so validation should pass.
        _validate_webhook_target("https://example.com/hook")


class TestFireWebhook:
    def test_skipped_when_url_unset(self):
        with patch("linkedin.webhooks.WEBHOOK_URL", ""):
            # Should not raise, should not attempt any network call.
            with patch("linkedin.webhooks._post") as mock_post:
                fire_webhook("message.sent", {"public_id": "alice"})
                mock_post.assert_not_called()

    def test_rejects_ssrf_target_without_posting(self):
        with patch("linkedin.webhooks.WEBHOOK_URL", "http://127.0.0.1/hook"):
            with patch("linkedin.webhooks._post") as mock_post:
                fire_webhook("message.sent", {"public_id": "alice"})
                mock_post.assert_not_called()

    def test_posts_to_valid_target(self):
        with patch("linkedin.webhooks.WEBHOOK_URL", "https://example.com/hook"):
            with patch("linkedin.webhooks._post", return_value=200) as mock_post:
                fire_webhook("message.sent", {"public_id": "alice"})
                mock_post.assert_called_once()
                args, _ = mock_post.call_args
                assert args[0] == "https://example.com/hook"

    def test_swallows_urlerror_after_retries(self):
        import urllib.error

        with patch("linkedin.webhooks.WEBHOOK_URL", "https://example.com/hook"):
            with patch(
                "linkedin.webhooks._post",
                side_effect=urllib.error.URLError("boom"),
            ):
                # Must not raise — webhook delivery is best-effort.
                fire_webhook("message.sent", {"public_id": "alice"})

    def test_propagates_unexpected_exceptions(self):
        with patch("linkedin.webhooks.WEBHOOK_URL", "https://example.com/hook"):
            with patch("linkedin.webhooks._post", side_effect=RuntimeError("bug")):
                with pytest.raises(RuntimeError, match="bug"):
                    fire_webhook("message.sent", {"public_id": "alice"})
