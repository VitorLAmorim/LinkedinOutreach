# tests/test_daemon_account_swap.py
"""Daemon hot-swap tests for the pool-mode account + runtime proxy change."""

import pytest

from linkedin.accounts.pool import claim_next_account
from linkedin.daemon import _PlaceholderAccount, _refresh_account_binding, _refresh_proxy
from linkedin.models import Campaign, LinkedInAccount


class _RecordingSession:
    """AccountSession test double that records close / ensure_browser calls."""

    def __init__(self, account, proxy="", has_browser=True):
        self.account = account
        self.campaign = None
        self.page = object() if has_browser else None
        self.context = object() if has_browser else None
        self.browser = object() if has_browser else None
        self.playwright = object() if has_browser else None
        self._launched_with_proxy = proxy
        self._worker_id = ""
        self.closed_count = 0
        self.ensured_count = 0

    def close(self):
        self.closed_count += 1
        self.page = self.context = self.browser = self.playwright = None
        self._launched_with_proxy = None

    def ensure_browser(self):
        self.ensured_count += 1
        self.page = object()
        self.context = object()
        self.browser = object()
        self.playwright = object()
        self._launched_with_proxy = self.account.proxy_url

    def invalidate_campaigns_cache(self):
        pass

    def swap_account(self, new_account):
        assert self.page is None  # must be closed first
        self.account = new_account

    def bind_worker_id(self, worker_id):
        self._worker_id = worker_id


def _make_account(username, *, with_active_campaign=True, proxy_url=""):
    account = LinkedInAccount.objects.create(
        username=username,
        linkedin_username=f"{username}@example.com",
        linkedin_password="x",
        proxy_url=proxy_url,
    )
    if with_active_campaign:
        Campaign.objects.create(name=f"c-{username}", account=account, active=True)
    return account


@pytest.mark.django_db
class TestRefreshAccountBinding:
    def test_noop_when_worker_id_empty(self):
        """Pinned mode (no worker_id) must never hot-swap."""
        account = _make_account("alice")
        session = _RecordingSession(account)
        swapped = _refresh_account_binding(session, worker_id="")
        assert swapped is False
        assert session.closed_count == 0

    def test_noop_when_account_still_eligible(self):
        account = _make_account("bob")
        claim_next_account("worker-1")
        session = _RecordingSession(LinkedInAccount.objects.get(pk=account.pk))
        swapped = _refresh_account_binding(session, worker_id="worker-1")
        assert swapped is False
        assert session.closed_count == 0

    def test_swaps_when_current_account_loses_campaign(self):
        old = _make_account("carol")
        new = _make_account("dave")
        claim_next_account("worker-1")  # claims carol (lower pk)
        Campaign.objects.filter(account=old).update(active=False)
        session = _RecordingSession(LinkedInAccount.objects.get(pk=old.pk))
        swapped = _refresh_account_binding(session, worker_id="worker-1")
        assert swapped is True
        assert session.closed_count == 1
        assert session.ensured_count == 1
        assert session.account.pk == new.pk

    def test_idles_on_placeholder_when_no_eligible(self):
        account = _make_account("eve")
        claim_next_account("worker-1")
        Campaign.objects.filter(account=account).update(active=False)
        session = _RecordingSession(LinkedInAccount.objects.get(pk=account.pk))
        swapped = _refresh_account_binding(session, worker_id="worker-1")
        assert swapped is True
        assert session.closed_count == 1
        assert isinstance(session.account, _PlaceholderAccount)
        assert session.ensured_count == 0  # no browser launch when idle

    def test_rebinds_when_claim_stolen(self):
        """If someone else grabbed our row (stale takeover), we release + rebind."""
        old = _make_account("frank")
        new = _make_account("grace")
        claim_next_account("worker-1")  # claims frank
        # Simulate worker-2 taking over frank while worker-1 was busy
        LinkedInAccount.objects.filter(pk=old.pk).update(claimed_by="worker-2")
        # Prior heartbeat was set, so stealing is only detectable via ownership check
        session = _RecordingSession(LinkedInAccount.objects.get(pk=old.pk))
        swapped = _refresh_account_binding(session, worker_id="worker-1")
        assert swapped is True
        assert session.account.pk == new.pk  # claimed grace as the next eligible


@pytest.mark.django_db
class TestRefreshProxy:
    def test_noop_when_no_browser(self):
        account = _make_account("hank", proxy_url="http://a:1")
        session = _RecordingSession(account, proxy="http://a:1", has_browser=False)
        assert _refresh_proxy(session) is False
        assert session.ensured_count == 0

    def test_noop_when_proxy_unchanged(self):
        account = _make_account("irene", proxy_url="http://a:1")
        session = _RecordingSession(account, proxy="http://a:1")
        assert _refresh_proxy(session) is False
        assert session.closed_count == 0

    def test_restarts_when_proxy_url_changed_in_db(self):
        account = _make_account("jack", proxy_url="http://a:1")
        session = _RecordingSession(account, proxy="http://a:1")
        LinkedInAccount.objects.filter(pk=account.pk).update(proxy_url="http://b:2")
        assert _refresh_proxy(session) is True
        assert session.closed_count == 1
        assert session.ensured_count == 1
        assert session._launched_with_proxy == "http://b:2"

    def test_restarts_when_proxy_cleared(self):
        account = _make_account("kim", proxy_url="http://a:1")
        session = _RecordingSession(account, proxy="http://a:1")
        LinkedInAccount.objects.filter(pk=account.pk).update(proxy_url="")
        assert _refresh_proxy(session) is True
        assert session.closed_count == 1
