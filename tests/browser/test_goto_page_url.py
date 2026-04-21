# tests/browser/test_goto_page_url.py
"""Regression tests for goto_page's URL-path matcher.

A worker launched with stale cookies gets redirected by LinkedIn to:

    https://www.linkedin.com/uas/login?session_redirect=https%3A%2F%2Fwww.linkedin.com%2Ffeed%2F

The old matcher checked ``"/feed" in unquote(full_url)`` which false-positives
on that URL because the query string contains `/feed/` after unquoting. The
result was that ``start_browser_session`` accepted the login page as
"successfully navigated to /feed", and the daemon then crashed with a
401 on the first Voyager API call.

The fix: match against only the URL's path component.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.browser.nav import _url_path, goto_page


class TestUrlPathHelper:
    def test_plain_path(self):
        assert _url_path("https://www.linkedin.com/feed/") == "/feed/"

    def test_ignores_query_string(self):
        url = (
            "https://www.linkedin.com/uas/login"
            "?session_redirect=https%3A%2F%2Fwww.linkedin.com%2Ffeed%2F"
        )
        assert _url_path(url) == "/uas/login"
        assert "/feed" not in _url_path(url)

    def test_ignores_fragment(self):
        assert _url_path("https://www.linkedin.com/feed/#messaging") == "/feed/"

    def test_unquotes_percent_encoded_segments(self):
        url = "https://www.linkedin.com/in/john%20doe/"
        assert _url_path(url) == "/in/john doe/"


def _fake_session(page_url: str):
    page = MagicMock(name="page")
    page.url = page_url
    # wait_for_url evaluates the predicate immediately — simulate a timeout
    # so goto_page falls through to the post-check that raises on mismatch.
    page.wait_for_url.side_effect = PlaywrightTimeoutError("no match")
    return SimpleNamespace(page=page, wait=lambda *a, **kw: None)


class TestGotoPageRejectsLoginRedirect:
    def test_saved_session_invalid_raises_runtime_error(self):
        """The exact LinkedIn redirect the user hit must be rejected."""
        session = _fake_session(
            "https://www.linkedin.com/uas/login"
            "?session_redirect=https%3A%2F%2Fwww.linkedin.com%2Ffeed%2F"
        )

        with pytest.raises(RuntimeError, match="Saved session invalid"):
            goto_page(
                session,
                action=lambda: None,
                expected_url_pattern="/feed",
                timeout=1,
                error_message="Saved session invalid",
            )

    def test_genuine_feed_navigation_passes(self):
        session = _fake_session("https://www.linkedin.com/feed/")
        # wait_for_url returning successfully simulates the predicate matching
        session.page.wait_for_url.side_effect = None
        session.page.wait_for_url.return_value = None

        # No exception → goto_page succeeded
        goto_page(
            session,
            action=lambda: None,
            expected_url_pattern="/feed",
            timeout=1,
            error_message="should not raise",
        )

    def test_profile_url_with_query_params_still_matches_path(self):
        # Profile pages sometimes carry tracking params; the matcher should
        # still accept them because the path starts with /in/<pid>.
        session = _fake_session(
            "https://www.linkedin.com/in/alice/?trk=public_profile_browsemap"
        )
        session.page.wait_for_url.side_effect = None
        session.page.wait_for_url.return_value = None

        goto_page(
            session,
            action=lambda: None,
            expected_url_pattern="/in/alice",
            timeout=1,
            error_message="should not raise",
        )
