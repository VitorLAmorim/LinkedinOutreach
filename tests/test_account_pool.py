# tests/test_account_pool.py
"""Unit tests for linkedin/accounts/pool.py (claim/release/heartbeat/eligibility)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from linkedin.accounts.pool import (
    STALE_CLAIM_TIMEOUT,
    claim_next_account,
    heartbeat,
    is_still_eligible,
    release_account,
)
from linkedin.models import Campaign, LinkedInAccount


def _make_account(username: str, *, with_active_campaign: bool = True) -> LinkedInAccount:
    account = LinkedInAccount.objects.create(
        username=username,
        linkedin_username=f"{username}@example.com",
        linkedin_password="x",
    )
    if with_active_campaign:
        Campaign.objects.create(name=f"campaign-{username}", account=account, active=True)
    return account


@pytest.mark.django_db
class TestClaimNextAccount:
    def test_unclaimed_account_is_claimed(self):
        account = _make_account("alice")
        claimed = claim_next_account("worker-1")
        assert claimed is not None
        assert claimed.pk == account.pk
        assert claimed.claimed_by == "worker-1"
        assert claimed.claimed_at is not None
        assert claimed.last_heartbeat is not None

    def test_account_without_active_campaign_is_ineligible(self):
        _make_account("bob", with_active_campaign=False)
        assert claim_next_account("worker-1") is None

    def test_archived_account_is_ineligible(self):
        account = _make_account("carol")
        account.is_archived = True
        account.save()
        assert claim_next_account("worker-1") is None

    def test_inactive_account_is_ineligible(self):
        account = _make_account("dave")
        account.active = False
        account.save()
        assert claim_next_account("worker-1") is None

    def test_already_claimed_account_not_given_to_other_worker(self):
        _make_account("eve")
        first = claim_next_account("worker-1")
        assert first is not None
        assert claim_next_account("worker-2") is None

    def test_re_claim_by_same_worker_is_idempotent(self):
        _make_account("frank")
        a = claim_next_account("worker-1")
        b = claim_next_account("worker-1")
        assert a is not None and b is not None
        assert a.pk == b.pk

    def test_two_workers_get_different_accounts(self):
        _make_account("grace")
        _make_account("harry")
        a = claim_next_account("worker-1")
        b = claim_next_account("worker-2")
        assert a is not None and b is not None
        assert a.pk != b.pk

    def test_stale_claim_can_be_stolen(self):
        account = _make_account("ivy")
        claim_next_account("worker-1")
        LinkedInAccount.objects.filter(pk=account.pk).update(
            last_heartbeat=timezone.now() - timedelta(seconds=STALE_CLAIM_TIMEOUT + 10),
        )
        stolen = claim_next_account("worker-2")
        assert stolen is not None
        assert stolen.pk == account.pk
        assert stolen.claimed_by == "worker-2"


@pytest.mark.django_db
class TestReleaseAccount:
    def test_release_clears_claim(self):
        _make_account("jack")
        claimed = claim_next_account("worker-1")
        assert release_account(claimed, "worker-1") is True
        claimed.refresh_from_db()
        assert claimed.claimed_by == ""
        assert claimed.claimed_at is None
        assert claimed.last_heartbeat is None

    def test_release_by_wrong_worker_is_noop(self):
        _make_account("kate")
        claimed = claim_next_account("worker-1")
        assert release_account(claimed, "worker-999") is False
        claimed.refresh_from_db()
        assert claimed.claimed_by == "worker-1"


@pytest.mark.django_db
class TestHeartbeat:
    def test_heartbeat_updates_timestamp(self):
        _make_account("leo")
        claimed = claim_next_account("worker-1")
        old_hb = claimed.last_heartbeat
        LinkedInAccount.objects.filter(pk=claimed.pk).update(
            last_heartbeat=old_hb - timedelta(seconds=30),
        )
        assert heartbeat(claimed, "worker-1") is True
        claimed.refresh_from_db()
        assert claimed.last_heartbeat > old_hb - timedelta(seconds=30)

    def test_heartbeat_fails_if_claim_stolen(self):
        _make_account("mia")
        claimed = claim_next_account("worker-1")
        LinkedInAccount.objects.filter(pk=claimed.pk).update(claimed_by="worker-thief")
        assert heartbeat(claimed, "worker-1") is False


@pytest.mark.django_db
class TestIsStillEligible:
    def test_eligible_when_active_campaign(self):
        account = _make_account("nina")
        assert is_still_eligible(account) is True

    def test_ineligible_when_campaign_deactivated(self):
        account = _make_account("oliver")
        Campaign.objects.filter(account=account).update(active=False)
        assert is_still_eligible(account) is False

    def test_ineligible_when_archived(self):
        account = _make_account("paula")
        LinkedInAccount.objects.filter(pk=account.pk).update(is_archived=True)
        assert is_still_eligible(account) is False

    def test_ineligible_when_deleted(self):
        account = _make_account("quinn")
        pk = account.pk
        LinkedInAccount.objects.filter(pk=pk).delete()
        # rebuild a lightweight stub with the old pk so is_still_eligible queries
        stub = LinkedInAccount(pk=pk, username="quinn")
        assert is_still_eligible(stub) is False
