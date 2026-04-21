# tests/browser/test_login_heartbeat.py
"""Regression tests for the cooperative heartbeat during long login waits.

These exist because a real-world captcha-wait race caused two worker
containers to simultaneously operate the same LinkedIn account: while
``playwright_login`` was blocked for several minutes on a captcha page,
the daemon loop's 180s stale-claim timeout kicked in and another worker
stole the claim. The cooperative heartbeat in ``_wait_for_login_redirect``
prevents that.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.browser.login import _wait_for_login_redirect
from linkedin.exceptions import LoginFailed


def _fake_session(worker_id: str = "worker-xyz", account_pk: int = 1):
    """Build a minimal session stub with enough surface for the helper."""
    account = SimpleNamespace(pk=account_pk, username="alice")
    page = MagicMock(name="page")
    page.url = "https://www.linkedin.com/checkpoint/challenge/foo"
    return SimpleNamespace(
        page=page,
        account=account,
        _worker_id=worker_id,
    )


def test_returns_immediately_when_predicate_matches_first_burst():
    session = _fake_session()
    # wait_for_url returns (not raise) → predicate matched in this burst
    session.page.wait_for_url.return_value = None

    with patch("linkedin.accounts.pool.heartbeat") as mock_heartbeat:
        _wait_for_login_redirect(
            session,
            predicate=lambda url: "/feed" in url,
            total_timeout_ms=60_000,
        )

    # Happy path: only one burst, no cooperative heartbeat needed
    assert session.page.wait_for_url.call_count == 1
    mock_heartbeat.assert_not_called()


def test_heartbeats_between_bursts_during_long_wait():
    session = _fake_session()
    # Three timeouts then a successful match → two heartbeats between the
    # three timing-out bursts, then the fourth burst matches (no hb after).
    session.page.wait_for_url.side_effect = [
        PlaywrightTimeoutError("burst 1"),
        PlaywrightTimeoutError("burst 2"),
        PlaywrightTimeoutError("burst 3"),
        None,  # matched
    ]

    with patch(
        "linkedin.accounts.pool.heartbeat", return_value=True,
    ) as mock_heartbeat:
        _wait_for_login_redirect(
            session,
            predicate=lambda url: "/feed" in url,
            total_timeout_ms=60_000,
        )

    assert session.page.wait_for_url.call_count == 4
    # One heartbeat per burst that timed out (3), none after the matching one
    assert mock_heartbeat.call_count == 3


def test_raises_login_failed_when_claim_stolen_mid_wait():
    session = _fake_session()
    session.page.wait_for_url.side_effect = PlaywrightTimeoutError("still waiting")

    with patch(
        "linkedin.accounts.pool.heartbeat", return_value=False,
    ) as mock_heartbeat:
        with pytest.raises(LoginFailed, match="stolen"):
            _wait_for_login_redirect(
                session,
                predicate=lambda url: "/feed" in url,
                total_timeout_ms=60_000,
            )

    # First burst timed out, first heartbeat returned False → bail out
    mock_heartbeat.assert_called_once()


def test_returns_on_total_timeout_without_raising():
    # Exhausted budget with no match — helper returns, caller checks URL.
    session = _fake_session()
    session.page.wait_for_url.side_effect = PlaywrightTimeoutError("forever")

    with patch("linkedin.accounts.pool.heartbeat", return_value=True):
        _wait_for_login_redirect(
            session,
            predicate=lambda url: "/feed" in url,
            total_timeout_ms=30_000,  # 2 bursts of 15s
        )

    # Two bursts used up the whole budget
    assert session.page.wait_for_url.call_count == 2


def test_skips_heartbeat_in_pinned_mode():
    # Pinned mode: no worker_id, no pool claim to heartbeat.
    session = _fake_session(worker_id="")
    session.page.wait_for_url.side_effect = [
        PlaywrightTimeoutError("burst 1"),
        None,
    ]

    with patch("linkedin.accounts.pool.heartbeat") as mock_heartbeat:
        _wait_for_login_redirect(
            session,
            predicate=lambda url: "/feed" in url,
            total_timeout_ms=60_000,
        )

    mock_heartbeat.assert_not_called()


class TestPlaywrightLoginErrorConversion:
    """playwright_login must convert Playwright selector failures to LoginFailed.

    The real-world trigger: LinkedIn A/B-tested a login layout where
    input#username wasn't immediately visible. Locator.type timed out with
    a bare PlaywrightTimeoutError that propagated all the way up and
    crashed the worker, leaving the pool claim orphaned. Downstream recovery
    paths only catch LoginFailed, so converting here is the fix.
    """

    def test_playwright_timeout_becomes_login_failed(self):
        from linkedin.browser import login as login_module

        session = SimpleNamespace(
            page=MagicMock(),
            account=SimpleNamespace(
                username="alice", linkedin_username="alice@example.com",
                linkedin_password="pw",
            ),
            _worker_id="w-1",
            wait=lambda *a, **kw: None,
        )

        def raise_timeout(*_a, **_kw):
            raise PlaywrightTimeoutError("Locator.type: no input#username")

        with patch.object(login_module, "goto_page", lambda *a, **kw: None), \
             patch.object(login_module, "human_type", side_effect=raise_timeout):
            with pytest.raises(LoginFailed, match="TimeoutError"):
                login_module.playwright_login(session)

    def test_runtime_error_from_goto_page_becomes_login_failed(self):
        from linkedin.browser import login as login_module

        session = SimpleNamespace(
            page=MagicMock(),
            account=SimpleNamespace(
                username="alice", linkedin_username="alice@example.com",
                linkedin_password="pw",
            ),
            _worker_id="w-1",
            wait=lambda *a, **kw: None,
        )

        def raise_runtime(*_a, **_kw):
            raise RuntimeError("Saved session invalid → expected '/login'")

        with patch.object(login_module, "goto_page", side_effect=raise_runtime):
            with pytest.raises(LoginFailed, match="Login navigation failed"):
                login_module.playwright_login(session)

    def test_login_failed_passes_through_unwrapped(self):
        # A LoginFailed raised inside the body (e.g. from _wait_for_login_redirect
        # when the claim is stolen) must NOT be double-wrapped.
        from linkedin.browser import login as login_module

        session = SimpleNamespace(
            page=MagicMock(),
            account=SimpleNamespace(
                username="alice", linkedin_username="alice@example.com",
                linkedin_password="pw",
            ),
            _worker_id="w-1",
            wait=lambda *a, **kw: None,
        )

        def raise_login_failed(*_a, **_kw):
            raise LoginFailed("claim stolen")

        with patch.object(login_module, "goto_page", lambda *a, **kw: None), \
             patch.object(login_module, "human_type", side_effect=raise_login_failed):
            with pytest.raises(LoginFailed, match="^claim stolen$"):
                login_module.playwright_login(session)
