"""Tests for the FastMCP tool layer.

These verify that:
1. The tools are registered with the right names.
2. Each tool's input schema is strict (no NL fields, all params named & typed).
3. Tools route through the injected client and propagate errors as
   structured ``{"error": {...}}`` payloads (code-mode contract).
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from mcp_server import server as srv
from mcp_server.client import OpenOutreachAPIError


@pytest.fixture
def mock_client(monkeypatch):
    client = MagicMock()
    srv.set_client(client)
    yield client
    srv.set_client(None)


def _list_tools():
    return asyncio.run(srv.mcp.list_tools())


# ── Registration & code-mode strictness ─────────────────────────────────────


EXPECTED_TOOLS = {
    "list_accounts",
    "create_account",
    "get_account",
    "update_account",
    "archive_account",
    "list_campaigns",
    "create_campaign",
    "get_campaign",
    "update_campaign",
    "delete_campaign",
    "activate_campaign",
    "deactivate_campaign",
    "list_campaign_deals",
    "get_campaign_stats",
    "get_lead",
    "list_lead_deals",
    "get_deal",
    "send_message",
    "get_conversation",
    "get_task",
}


def test_all_expected_tools_registered():
    names = {t.name for t in _list_tools()}
    assert EXPECTED_TOOLS.issubset(names), f"Missing: {EXPECTED_TOOLS - names}"


def test_no_natural_language_intent_fields():
    """Code-mode rule: no tool may accept fields like 'intent', 'action', 'description', 'instruction'."""
    forbidden = {"intent", "action", "instruction", "describe", "describe_what", "prompt"}
    violations = []
    for tool in _list_tools():
        props = (tool.inputSchema or {}).get("properties", {}) or {}
        for field_name in props:
            if field_name.lower() in forbidden:
                violations.append((tool.name, field_name))
    assert not violations, f"NL fields found: {violations}"


def test_send_message_schema_is_strict():
    sm = next(t for t in _list_tools() if t.name == "send_message")
    schema = sm.inputSchema
    props = schema["properties"]
    assert set(props.keys()) == {"campaign_id", "public_id", "message"}
    assert props["campaign_id"]["type"] == "integer"
    assert props["public_id"]["type"] == "string"
    assert props["message"]["type"] == "string"
    assert set(schema["required"]) == {"campaign_id", "public_id", "message"}


def test_activate_campaign_schema_takes_only_id():
    tool = next(t for t in _list_tools() if t.name == "activate_campaign")
    props = tool.inputSchema["properties"]
    assert set(props.keys()) == {"campaign_id"}
    assert props["campaign_id"]["type"] == "integer"


# ── Tool dispatch routes through the client ─────────────────────────────────


def test_send_message_calls_client_with_strict_args(mock_client):
    mock_client.send_message.return_value = {"task_id": 7, "status": "pending"}
    result = srv.send_message(campaign_id=1, public_id="alice", message="Hi")
    mock_client.send_message.assert_called_once_with(1, "alice", "Hi")
    assert result == {"task_id": 7, "status": "pending"}


def test_activate_campaign_calls_client(mock_client):
    mock_client.activate_campaign.return_value = {
        "campaign_id": 9, "active": True, "deactivated_campaign_id": 3,
    }
    result = srv.activate_campaign(campaign_id=9)
    mock_client.activate_campaign.assert_called_once_with(9)
    assert result["deactivated_campaign_id"] == 3


def test_list_accounts_wraps_in_envelope(mock_client):
    mock_client.list_accounts.return_value = [{"id": 1, "username": "a"}]
    result = srv.list_accounts()
    assert result == {"accounts": [{"id": 1, "username": "a"}]}


def test_list_campaigns_passes_filters(mock_client):
    mock_client.list_campaigns.return_value = []
    srv.list_campaigns(account_id=2, active=True)
    mock_client.list_campaigns.assert_called_once_with(account_id=2, active=True)


def test_api_error_returns_structured_error(mock_client):
    mock_client.get_account.side_effect = OpenOutreachAPIError(404, {"error": "missing"})
    result = srv.get_account(account_id=99)
    assert result == {"error": {"status_code": 404, "body": {"error": "missing"}}}


def test_create_account_omits_unset_optionals(mock_client):
    mock_client.create_account.return_value = {"id": 1}
    srv.create_account(
        username="alice",
        linkedin_username="alice@example.com",
        linkedin_password="pw",
    )
    call_kwargs = mock_client.create_account.call_args.kwargs
    assert call_kwargs["username"] == "alice"
    assert call_kwargs["linkedin_username"] == "alice@example.com"
    assert call_kwargs["linkedin_password"] == "pw"
    # Optional fields are passed as None — the client decides what to send.
    assert call_kwargs["connect_daily_limit"] is None
    assert call_kwargs["subscribe_newsletter"] is None
