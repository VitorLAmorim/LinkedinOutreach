"""Daemon hot-swap test: activating a new campaign mid-loop swaps the worker."""
import pytest

from django.utils import timezone

from linkedin.daemon import (
    cancel_pending_tasks_for_campaign,
    get_active_campaign_id,
    _refresh_active_campaign,
)
from linkedin.models import Campaign, LinkedInAccount, Task


@pytest.mark.django_db
class TestActiveCampaignTracking:
    def test_get_active_campaign_id_none(self):
        account = LinkedInAccount.objects.create(
            username="acc1",
            linkedin_username="acc1@example.com",
            linkedin_password="x",
        )
        assert get_active_campaign_id(account) is None

    def test_get_active_campaign_id_returns_active(self):
        account = LinkedInAccount.objects.create(
            username="acc2",
            linkedin_username="acc2@example.com",
            linkedin_password="x",
        )
        Campaign.objects.create(name="Idle", account=account, active=False)
        active = Campaign.objects.create(name="Active", account=account, active=True)
        assert get_active_campaign_id(account) == active.pk


@pytest.mark.django_db
class TestCancelPendingTasks:
    def test_cancels_only_target_campaign(self):
        account = LinkedInAccount.objects.create(
            username="acc3",
            linkedin_username="acc3@example.com",
            linkedin_password="x",
        )
        a = Campaign.objects.create(name="A", account=account, active=False)
        b = Campaign.objects.create(name="B", account=account, active=False)

        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            scheduled_at=timezone.now(),
            payload={"campaign_id": a.pk},
        )
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            scheduled_at=timezone.now(),
            payload={"campaign_id": b.pk},
        )

        cancelled = cancel_pending_tasks_for_campaign(a.pk)
        assert cancelled == 1
        assert Task.objects.filter(payload__campaign_id=a.pk, status=Task.Status.CANCELLED).count() == 1
        assert Task.objects.filter(payload__campaign_id=b.pk, status=Task.Status.PENDING).count() == 1


class _StubSession:
    def __init__(self, account):
        self.account = account
        self.campaign = None
        self._campaigns_cache = None

    @property
    def campaigns(self):
        if self._campaigns_cache is None:
            self._campaigns_cache = list(Campaign.objects.filter(account=self.account, active=True))
        return self._campaigns_cache

    def invalidate_campaigns_cache(self):
        self._campaigns_cache = None


@pytest.mark.django_db
class TestRefreshActiveCampaign:
    def _account(self, name="hotswap"):
        return LinkedInAccount.objects.create(
            username=name,
            linkedin_username=f"{name}@example.com",
            linkedin_password="x",
        )

    def test_no_change(self):
        account = self._account("hs1")
        active = Campaign.objects.create(name="Stable", account=account, active=True)
        session = _StubSession(account)

        new_id, qualifiers = _refresh_active_campaign(session, active.pk, {}, kit_model=None)
        assert new_id == active.pk
        assert qualifiers == {}

    def test_swap_cancels_old_and_seeds_new(self):
        account = self._account("hs2")
        old = Campaign.objects.create(name="Old", account=account, active=False)
        new = Campaign.objects.create(name="New", account=account, active=True)

        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            scheduled_at=timezone.now(),
            payload={"campaign_id": old.pk},
        )

        session = _StubSession(account)
        new_id, _ = _refresh_active_campaign(session, old.pk, {}, kit_model=None)

        assert new_id == new.pk
        assert Task.objects.filter(
            payload__campaign_id=old.pk, status=Task.Status.CANCELLED,
        ).exists()
        assert Task.objects.filter(
            payload__campaign_id=new.pk, task_type=Task.TaskType.CONNECT,
        ).exists()
        assert session.campaign == new

    def test_swap_to_idle(self):
        account = self._account("hs3")
        old = Campaign.objects.create(name="Going away", account=account, active=False)
        session = _StubSession(account)

        new_id, qualifiers = _refresh_active_campaign(session, old.pk, {}, kit_model=None)
        assert new_id is None
        assert qualifiers == {}
        assert session.campaign is None
