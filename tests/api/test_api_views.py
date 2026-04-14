# tests/api/test_api_views.py
import json

import pytest
from unittest.mock import patch, MagicMock

from django.test import Client
from django.utils import timezone

from linkedin.models import Campaign, Task


API_KEY = "test-api-key-123"


@pytest.fixture
def api_client():
    return Client()


@pytest.fixture
def auth_headers():
    return {"HTTP_AUTHORIZATION": f"Bearer {API_KEY}"}


@pytest.fixture
def account(db):
    from linkedin.models import LinkedInAccount
    acc, _ = LinkedInAccount.objects.get_or_create(
        username="apitestuser",
        defaults={
            "linkedin_username": "apitest@example.com",
            "linkedin_password": "secret",
        },
    )
    return acc


@pytest.fixture
def campaign(db, account):
    return Campaign.objects.filter(account=account).first() or Campaign.objects.create(
        name="Test Campaign",
        account=account,
        active=True,
    )


@pytest.fixture
def lead(db):
    from linkedin.db.leads import create_enriched_lead
    url = "https://www.linkedin.com/in/alice/"
    profile = {"first_name": "Alice", "last_name": "Smith", "headline": "Engineer"}
    create_enriched_lead(MagicMock(), url, profile)
    from crm.models import Lead
    return Lead.objects.get(public_identifier="alice")


@pytest.mark.django_db
class TestSendMessageView:
    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_enqueues_task(self, api_client, auth_headers, campaign, lead):
        resp = api_client.post(
            "/api/messages/send/",
            data=json.dumps({
                "campaign_id": campaign.pk,
                "public_id": "alice",
                "message": "Hello Alice!",
            }),
            content_type="application/json",
            **auth_headers,
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "pending"
        assert Task.objects.filter(pk=body["task_id"], task_type=Task.TaskType.SEND_MESSAGE).exists()

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_rejects_missing_fields(self, api_client, auth_headers):
        resp = api_client.post(
            "/api/messages/send/",
            data=json.dumps({"campaign_id": 1}),
            content_type="application/json",
            **auth_headers,
        )
        assert resp.status_code == 400

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_rejects_invalid_campaign(self, api_client, auth_headers, lead):
        resp = api_client.post(
            "/api/messages/send/",
            data=json.dumps({
                "campaign_id": 99999,
                "public_id": "alice",
                "message": "Hi",
            }),
            content_type="application/json",
            **auth_headers,
        )
        assert resp.status_code == 404

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_rejects_invalid_lead(self, api_client, auth_headers, campaign):
        resp = api_client.post(
            "/api/messages/send/",
            data=json.dumps({
                "campaign_id": campaign.pk,
                "public_id": "nonexistent",
                "message": "Hi",
            }),
            content_type="application/json",
            **auth_headers,
        )
        assert resp.status_code == 404

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_rejects_without_auth(self, api_client):
        resp = api_client.post(
            "/api/messages/send/",
            data=json.dumps({"campaign_id": 1, "public_id": "x", "message": "hi"}),
            content_type="application/json",
        )
        assert resp.status_code == 401

    @patch("linkedin.api_views.API_KEY", "")
    def test_503_when_api_key_not_configured(self, api_client, auth_headers):
        resp = api_client.post(
            "/api/messages/send/",
            data=json.dumps({"campaign_id": 1, "public_id": "x", "message": "hi"}),
            content_type="application/json",
            **auth_headers,
        )
        assert resp.status_code == 503


@pytest.mark.django_db
class TestTaskStatusView:
    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_returns_task(self, api_client, auth_headers, campaign):
        task = Task.objects.create(
            task_type=Task.TaskType.SEND_MESSAGE,
            status=Task.Status.PENDING,
            scheduled_at=timezone.now(),
            payload={"campaign_id": campaign.pk},
        )
        resp = api_client.get(f"/api/tasks/{task.pk}/", **auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == task.pk
        assert body["status"] == "pending"

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_404_for_missing_task(self, api_client, auth_headers):
        resp = api_client.get("/api/tasks/99999/", **auth_headers)
        assert resp.status_code == 404


@pytest.mark.django_db
class TestConversationView:
    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_returns_messages(self, api_client, auth_headers, lead):
        from django.contrib.contenttypes.models import ContentType
        from chat.models import ChatMessage

        ct = ContentType.objects.get_for_model(lead)
        ChatMessage.objects.create(
            content_type=ct, object_id=lead.pk,
            content="Hello", is_outgoing=True,
            linkedin_urn="urn:li:msg:1",
            creation_date=timezone.now(),
        )

        resp = api_client.get("/api/messages/alice/", **auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["public_id"] == "alice"
        assert len(body["messages"]) == 1
        assert body["messages"][0]["text"] == "Hello"

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_404_for_missing_lead(self, api_client, auth_headers):
        resp = api_client.get("/api/messages/nonexistent/", **auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Accounts CRUD
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAccountsCollection:
    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_list_returns_accounts(self, api_client, auth_headers, account):
        resp = api_client.get("/api/accounts/", **auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        usernames = [a["username"] for a in body["accounts"]]
        assert "apitestuser" in usernames

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_create_account(self, api_client, auth_headers):
        resp = api_client.post(
            "/api/accounts/",
            data=json.dumps({
                "username": "newacct",
                "linkedin_username": "new@example.com",
                "linkedin_password": "pw",
            }),
            content_type="application/json",
            **auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["username"] == "newacct"
        assert body["active_campaign_id"] is None

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_create_account_validation(self, api_client, auth_headers):
        resp = api_client.post(
            "/api/accounts/",
            data=json.dumps({"username": "x"}),
            content_type="application/json",
            **auth_headers,
        )
        assert resp.status_code == 400

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_create_account_duplicate(self, api_client, auth_headers, account):
        resp = api_client.post(
            "/api/accounts/",
            data=json.dumps({
                "username": "apitestuser",
                "linkedin_username": "x@example.com",
                "linkedin_password": "x",
            }),
            content_type="application/json",
            **auth_headers,
        )
        assert resp.status_code == 409


@pytest.mark.django_db
class TestAccountDetail:
    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_get(self, api_client, auth_headers, account):
        resp = api_client.get(f"/api/accounts/{account.pk}/", **auth_headers)
        assert resp.status_code == 200
        assert resp.json()["username"] == "apitestuser"

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_patch_updates_fields(self, api_client, auth_headers, account):
        resp = api_client.patch(
            f"/api/accounts/{account.pk}/",
            data=json.dumps({"connect_daily_limit": 50}),
            content_type="application/json",
            **auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["connect_daily_limit"] == 50

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_delete_archives(self, api_client, auth_headers, account):
        resp = api_client.delete(f"/api/accounts/{account.pk}/", **auth_headers)
        assert resp.status_code == 200
        assert resp.json()["is_archived"] is True
        account.refresh_from_db()
        assert account.is_archived is True

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_get_404(self, api_client, auth_headers):
        resp = api_client.get("/api/accounts/99999/", **auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Campaigns CRUD
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCampaignsCollection:
    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_list(self, api_client, auth_headers, campaign):
        resp = api_client.get("/api/campaigns/", **auth_headers)
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()["campaigns"]]
        assert campaign.name in names

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_list_filtered_by_account(self, api_client, auth_headers, campaign, account):
        resp = api_client.get(f"/api/campaigns/?account_id={account.pk}", **auth_headers)
        assert resp.status_code == 200
        for c in resp.json()["campaigns"]:
            assert c["account_id"] == account.pk

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_create(self, api_client, auth_headers, account):
        resp = api_client.post(
            "/api/campaigns/",
            data=json.dumps({
                "name": "New Campaign",
                "account_id": account.pk,
                "campaign_objective": "Find designers",
            }),
            content_type="application/json",
            **auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "New Campaign"
        assert body["account_id"] == account.pk
        assert body["active"] is False

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_create_validation(self, api_client, auth_headers):
        resp = api_client.post(
            "/api/campaigns/",
            data=json.dumps({"name": "x"}),
            content_type="application/json",
            **auth_headers,
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestCampaignDetail:
    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_get_with_stats(self, api_client, auth_headers, campaign):
        resp = api_client.get(f"/api/campaigns/{campaign.pk}/", **auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == campaign.pk
        assert "stats" in body

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_patch(self, api_client, auth_headers, campaign):
        resp = api_client.patch(
            f"/api/campaigns/{campaign.pk}/",
            data=json.dumps({"booking_link": "https://cal.com/me"}),
            content_type="application/json",
            **auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["booking_link"] == "https://cal.com/me"

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_delete(self, api_client, auth_headers, account):
        c = Campaign.objects.create(name="Doomed", account=account)
        resp = api_client.delete(f"/api/campaigns/{c.pk}/", **auth_headers)
        assert resp.status_code == 200
        assert not Campaign.objects.filter(pk=c.pk).exists()


@pytest.mark.django_db
class TestCampaignActivate:
    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_activate_atomically_swaps(self, api_client, auth_headers, account):
        a = Campaign.objects.create(name="A", account=account, active=True)
        b = Campaign.objects.create(name="B", account=account, active=False)

        resp = api_client.post(f"/api/campaigns/{b.pk}/activate/", **auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["campaign_id"] == b.pk
        assert body["active"] is True
        assert body["deactivated_campaign_id"] == a.pk

        a.refresh_from_db()
        b.refresh_from_db()
        assert a.active is False
        assert b.active is True

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_activate_when_none_active(self, api_client, auth_headers, account):
        c = Campaign.objects.create(name="Solo", account=account, active=False)

        resp = api_client.post(f"/api/campaigns/{c.pk}/activate/", **auth_headers)
        assert resp.status_code == 200
        assert resp.json()["deactivated_campaign_id"] is None
        c.refresh_from_db()
        assert c.active is True

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_deactivate(self, api_client, auth_headers, account):
        c = Campaign.objects.create(name="Stop me", account=account, active=True)
        resp = api_client.post(f"/api/campaigns/{c.pk}/deactivate/", **auth_headers)
        assert resp.status_code == 200
        c.refresh_from_db()
        assert c.active is False


@pytest.mark.django_db
class TestCampaignDealsAndStats:
    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_deals_empty(self, api_client, auth_headers, campaign):
        resp = api_client.get(f"/api/campaigns/{campaign.pk}/deals/", **auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["deals"] == []
        assert body["total"] == 0

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_deals_with_filter(self, api_client, auth_headers, campaign, lead):
        from crm.models.deal import Deal
        from linkedin.enums import ProfileState

        Deal.objects.create(lead=lead, campaign=campaign, state=ProfileState.QUALIFIED)
        resp = api_client.get(
            f"/api/campaigns/{campaign.pk}/deals/?state=Qualified",
            **auth_headers,
        )
        assert resp.status_code == 200
        assert len(resp.json()["deals"]) == 1

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_stats(self, api_client, auth_headers, campaign):
        resp = api_client.get(f"/api/campaigns/{campaign.pk}/stats/", **auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "stats" in body


# ---------------------------------------------------------------------------
# Leads & deals reads
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLeadEndpoints:
    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_lead_detail(self, api_client, auth_headers, lead):
        resp = api_client.get("/api/leads/alice/", **auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["public_identifier"] == "alice"
        assert body["first_name"] == "Alice"

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_lead_404(self, api_client, auth_headers):
        resp = api_client.get("/api/leads/missing/", **auth_headers)
        assert resp.status_code == 404

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_lead_deals(self, api_client, auth_headers, lead, campaign):
        from crm.models.deal import Deal
        Deal.objects.create(lead=lead, campaign=campaign)
        resp = api_client.get("/api/leads/alice/deals/", **auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()["deals"]) == 1


@pytest.mark.django_db
class TestDealDetail:
    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_get_deal(self, api_client, auth_headers, lead, campaign):
        from crm.models.deal import Deal
        deal = Deal.objects.create(lead=lead, campaign=campaign)
        resp = api_client.get(f"/api/deals/{deal.pk}/", **auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == deal.pk

    @patch("linkedin.api_views.API_KEY", API_KEY)
    def test_404(self, api_client, auth_headers):
        resp = api_client.get("/api/deals/99999/", **auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Constraint: one active campaign per account
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOneActivePerAccountConstraint:
    def test_db_rejects_two_active(self, account):
        from django.db import IntegrityError, transaction

        Campaign.objects.create(name="One", account=account, active=True)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Campaign.objects.create(name="Two", account=account, active=True)
