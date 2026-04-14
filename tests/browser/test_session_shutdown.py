# tests/browser/test_session_shutdown.py
"""install_shutdown_handler must release the pool claim on SIGTERM.

This test exists because a refactor once stranded ``__del__`` and ``__repr__``
as local functions inside ``install_shutdown_handler``'s body, silently
disabling both class methods. A direct test of the handler body ensures the
release path still runs — the method-level regression would manifest as
``release_account`` never being called.
"""
from __future__ import annotations

import signal
from unittest.mock import patch

import pytest

from linkedin.browser.session import AccountSession, install_shutdown_handler


class _StubAccount:
    username = "stub"
    linkedin_username = "stub@example.com"


def test_session_has_class_level_repr_and_del():
    """Regression guard — __repr__/__del__ must live on AccountSession itself."""
    assert "__repr__" in AccountSession.__dict__
    assert "__del__" in AccountSession.__dict__


def test_repr_uses_username_not_linkedin_login():
    session = AccountSession(_StubAccount())
    assert repr(session) == "stub"


def test_shutdown_handler_releases_claim_and_exits():
    session = AccountSession(_StubAccount())
    session.bind_worker_id("worker-xyz")

    install_shutdown_handler(session)

    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler), "SIGTERM handler not installed"

    with patch("linkedin.accounts.pool.release_account") as mock_release, \
         patch.object(AccountSession, "close") as mock_close:
        with pytest.raises(SystemExit) as excinfo:
            handler(signal.SIGTERM, None)
        assert excinfo.value.code == 0

    mock_release.assert_called_once()
    released_account, released_worker = mock_release.call_args[0]
    assert released_worker == "worker-xyz"
    assert released_account is session.account
    mock_close.assert_called_once()


def test_shutdown_handler_skips_release_when_no_worker_id():
    session = AccountSession(_StubAccount())
    # No bind_worker_id → _worker_id is empty string (pinned mode).

    install_shutdown_handler(session)
    handler = signal.getsignal(signal.SIGTERM)

    with patch("linkedin.accounts.pool.release_account") as mock_release, \
         patch.object(AccountSession, "close"):
        with pytest.raises(SystemExit):
            handler(signal.SIGTERM, None)

    mock_release.assert_not_called()
