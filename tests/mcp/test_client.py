"""OpenOutreachClient unit tests with httpx.MockTransport.

These exercise the HTTP wire shape (path, method, body, params, headers)
without spinning up the Django app — keeps the MCP layer truly standalone.
"""
from __future__ import annotations

import json

import httpx
import pytest

from mcp_server.client import OpenOutreachAPIError, OpenOutreachClient


def _make_client(handler) -> OpenOutreachClient:
    transport = httpx.MockTransport(handler)
    return OpenOutreachClient(
        base_url="http://test",
        api_key="test-key",
        transport=transport,
    )


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("OPENOUTREACH_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENOUTREACH_API_KEY"):
        OpenOutreachClient(base_url="http://test", api_key="")


def test_bearer_header_sent():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"accounts": []})

    client = _make_client(handler)
    client.list_accounts()
    assert captured["auth"] == "Bearer test-key"


def test_non_2xx_raises_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client = _make_client(handler)
    with pytest.raises(OpenOutreachAPIError) as exc_info:
        client.get_account(99)
    err: OpenOutreachAPIError = exc_info.value
    assert err.status_code == 404
    assert err.body == {"error": "not found"}


# ── Accounts wire shape ──────────────────────────────────────────────────────


class TestAccountsWire:
    def test_list_accounts_get(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            return httpx.Response(200, json={"accounts": [{"id": 1}]})

        client = _make_client(handler)
        result = client.list_accounts()
        assert captured == {"method": "GET", "path": "/api/accounts/"}
        assert result == [{"id": 1}]

    def test_create_account_post_strict_body(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": 7, "username": "alice"})

        client = _make_client(handler)
        client.create_account(
            username="alice",
            linkedin_username="alice@example.com",
            linkedin_password="pw",
            connect_daily_limit=15,
        )
        assert captured["method"] == "POST"
        assert captured["path"] == "/api/accounts/"
        assert captured["body"] == {
            "username": "alice",
            "linkedin_username": "alice@example.com",
            "linkedin_password": "pw",
            "connect_daily_limit": 15,
        }

    def test_create_account_omits_unset_optionals(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={})

        client = _make_client(handler)
        client.create_account(
            username="x",
            linkedin_username="x@example.com",
            linkedin_password="pw",
        )
        assert "connect_daily_limit" not in captured["body"]
        assert "subscribe_newsletter" not in captured["body"]

    def test_update_account_patch(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": 5})

        client = _make_client(handler)
        client.update_account(5, connect_daily_limit=99)
        assert captured == {
            "method": "PATCH",
            "path": "/api/accounts/5/",
            "body": {"connect_daily_limit": 99},
        }

    def test_archive_account_delete(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            return httpx.Response(200, json={"id": 5, "is_archived": True})

        client = _make_client(handler)
        client.archive_account(5)
        assert captured == {"method": "DELETE", "path": "/api/accounts/5/"}


# ── Campaigns wire shape ─────────────────────────────────────────────────────


class TestCampaignsWire:
    def test_list_campaigns_with_filters(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"campaigns": []})

        client = _make_client(handler)
        client.list_campaigns(account_id=3, active=True)
        assert captured["params"] == {"account_id": "3", "active": "true"}

    def test_create_campaign_required_fields(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": 1})

        client = _make_client(handler)
        client.create_campaign(name="Q1", account_id=2, campaign_objective="Find designers")
        assert captured["body"] == {
            "name": "Q1",
            "account_id": 2,
            "campaign_objective": "Find designers",
        }

    def test_activate_campaign(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            return httpx.Response(200, json={
                "campaign_id": 9, "active": True, "deactivated_campaign_id": 3,
            })

        client = _make_client(handler)
        result = client.activate_campaign(9)
        assert captured == {"method": "POST", "path": "/api/campaigns/9/activate/"}
        assert result["deactivated_campaign_id"] == 3

    def test_deactivate_campaign(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return httpx.Response(200, json={"campaign_id": 9, "active": False})

        client = _make_client(handler)
        client.deactivate_campaign(9)
        assert captured["path"] == "/api/campaigns/9/deactivate/"

    def test_list_campaign_deals_with_state_filter(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json={"deals": [], "total": 0, "limit": 50, "offset": 10})

        client = _make_client(handler)
        client.list_campaign_deals(7, state="Connected", limit=50, offset=10)
        assert captured["path"] == "/api/campaigns/7/deals/"
        assert captured["params"] == {"limit": "50", "offset": "10", "state": "Connected"}

    def test_get_campaign_stats(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return httpx.Response(200, json={"campaign_id": 1, "stats": {"Qualified": 5}})

        client = _make_client(handler)
        result = client.get_campaign_stats(1)
        assert captured["path"] == "/api/campaigns/1/stats/"
        assert result["stats"]["Qualified"] == 5


# ── Leads / deals / messages wire shape ──────────────────────────────────────


class TestLeadsAndMessagesWire:
    def test_get_lead(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return httpx.Response(200, json={"public_identifier": "alice"})

        client = _make_client(handler)
        client.get_lead("alice")
        assert captured["path"] == "/api/leads/alice/"

    def test_send_message_post_body(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(202, json={"task_id": 42, "status": "pending"})

        client = _make_client(handler)
        result = client.send_message(campaign_id=1, public_id="alice", message="Hi")
        assert captured == {
            "method": "POST",
            "path": "/api/messages/send/",
            "body": {"campaign_id": 1, "public_id": "alice", "message": "Hi"},
        }
        assert result == {"task_id": 42, "status": "pending"}

    def test_get_conversation(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return httpx.Response(200, json={"public_id": "alice", "messages": []})

        client = _make_client(handler)
        client.get_conversation("alice")
        assert captured["path"] == "/api/messages/alice/"

    def test_get_task(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return httpx.Response(200, json={"task_id": 42, "status": "completed"})

        client = _make_client(handler)
        result = client.get_task(42)
        assert captured["path"] == "/api/tasks/42/"
        assert result["status"] == "completed"
