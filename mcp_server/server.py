"""FastMCP server exposing OpenOutreach REST endpoints as code-mode tools.

Code-mode contract — every tool here:
- accepts strictly typed, named arguments (IDs, enums, literal values),
- returns structured data (dict / list of dicts),
- has no free-form natural-language fields like ``intent`` or ``action``,
- documents the wire shape in its docstring.

The LLM client is expected to compose tool calls programmatically. Where a
tool has a free-text body (e.g. ``send_message.message``) the body is
treated as a literal payload, NOT as an instruction to be interpreted.

Run with ``python -m mcp_server`` (stdio) or ``python -m mcp_server --transport http``.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_server.client import OpenOutreachAPIError, OpenOutreachClient

mcp = FastMCP("openoutreach")

_client_instance: OpenOutreachClient | None = None


def get_client() -> OpenOutreachClient:
    """Lazily-constructed singleton — reads env vars on first access."""
    global _client_instance
    if _client_instance is None:
        _client_instance = OpenOutreachClient()
    return _client_instance


def set_client(client: OpenOutreachClient | None) -> None:
    """Inject a client (used by tests). Pass ``None`` to reset."""
    global _client_instance
    _client_instance = client


def _safe_call(fn, *args, **kwargs) -> Any:
    """Translate OpenOutreachAPIError into a structured error response.

    Code-mode clients can branch on the ``error`` key without parsing prose.
    """
    try:
        return fn(*args, **kwargs)
    except OpenOutreachAPIError as e:
        return {"error": {"status_code": e.status_code, "body": e.body}}


# ── Accounts ────────────────────────────────────────────────────────────────


@mcp.tool()
def list_accounts() -> dict:
    """List all non-archived LinkedIn accounts.

    Returns: ``{"accounts": [{"id", "username", "linkedin_username", "active",
    "is_archived", "active_campaign_id", ...}, ...]}``
    """
    return {"accounts": _safe_call(get_client().list_accounts)}


@mcp.tool()
def create_account(
    username: str,
    linkedin_username: str,
    linkedin_password: str,
    connect_daily_limit: int | None = None,
    connect_weekly_limit: int | None = None,
    follow_up_daily_limit: int | None = None,
    subscribe_newsletter: bool | None = None,
    legal_accepted: bool | None = None,
) -> dict:
    """Create a LinkedInAccount row.

    Args:
        username: Unique handle. Must match a worker container's LINKEDIN_PROFILE env var.
        linkedin_username: LinkedIn login email.
        linkedin_password: LinkedIn login password (stored in DB).
        connect_daily_limit: Optional override (default 20).
        connect_weekly_limit: Optional override (default 100).
        follow_up_daily_limit: Optional override (default 30).
        subscribe_newsletter: Optional newsletter opt-in.
        legal_accepted: Optional legal acceptance flag.

    Returns the new account dict. Does NOT spawn a container — pre-provision in local.yml.
    """
    return _safe_call(
        get_client().create_account,
        username=username,
        linkedin_username=linkedin_username,
        linkedin_password=linkedin_password,
        connect_daily_limit=connect_daily_limit,
        connect_weekly_limit=connect_weekly_limit,
        follow_up_daily_limit=follow_up_daily_limit,
        subscribe_newsletter=subscribe_newsletter,
        legal_accepted=legal_accepted,
    )


@mcp.tool()
def get_account(account_id: int) -> dict:
    """Fetch a single LinkedInAccount by integer id."""
    return _safe_call(get_client().get_account, account_id)


@mcp.tool()
def update_account(
    account_id: int,
    linkedin_username: str | None = None,
    linkedin_password: str | None = None,
    subscribe_newsletter: bool | None = None,
    connect_daily_limit: int | None = None,
    connect_weekly_limit: int | None = None,
    follow_up_daily_limit: int | None = None,
    active: bool | None = None,
    legal_accepted: bool | None = None,
) -> dict:
    """Patch one or more fields on a LinkedInAccount. Only supplied fields change."""
    return _safe_call(
        get_client().update_account,
        account_id,
        linkedin_username=linkedin_username,
        linkedin_password=linkedin_password,
        subscribe_newsletter=subscribe_newsletter,
        connect_daily_limit=connect_daily_limit,
        connect_weekly_limit=connect_weekly_limit,
        follow_up_daily_limit=follow_up_daily_limit,
        active=active,
        legal_accepted=legal_accepted,
    )


@mcp.tool()
def archive_account(account_id: int) -> dict:
    """Soft-delete an account (sets is_archived=True, active=False)."""
    return _safe_call(get_client().archive_account, account_id)


# ── Campaigns ───────────────────────────────────────────────────────────────


@mcp.tool()
def list_campaigns(
    account_id: int | None = None,
    active: bool | None = None,
) -> dict:
    """List campaigns. Filter by account_id and/or active flag.

    Returns: ``{"campaigns": [{"id", "name", "account_id", "active", ...}, ...]}``
    """
    return {"campaigns": _safe_call(get_client().list_campaigns, account_id=account_id, active=active)}


@mcp.tool()
def create_campaign(
    name: str,
    account_id: int,
    campaign_objective: str | None = None,
    product_docs: str | None = None,
    booking_link: str | None = None,
    seed_public_ids: list[str] | None = None,
    is_freemium: bool | None = None,
    action_fraction: float | None = None,
) -> dict:
    """Create a Campaign bound to an account.

    The campaign is created with active=False. Activate it with activate_campaign().
    """
    return _safe_call(
        get_client().create_campaign,
        name=name,
        account_id=account_id,
        campaign_objective=campaign_objective,
        product_docs=product_docs,
        booking_link=booking_link,
        seed_public_ids=seed_public_ids,
        is_freemium=is_freemium,
        action_fraction=action_fraction,
    )


@mcp.tool()
def get_campaign(campaign_id: int) -> dict:
    """Fetch a single Campaign with deal-state stats."""
    return _safe_call(get_client().get_campaign, campaign_id)


@mcp.tool()
def update_campaign(
    campaign_id: int,
    name: str | None = None,
    campaign_objective: str | None = None,
    product_docs: str | None = None,
    booking_link: str | None = None,
    seed_public_ids: list[str] | None = None,
    is_freemium: bool | None = None,
    action_fraction: float | None = None,
) -> dict:
    """Patch one or more fields on a Campaign. Only supplied fields change."""
    return _safe_call(
        get_client().update_campaign,
        campaign_id,
        name=name,
        campaign_objective=campaign_objective,
        product_docs=product_docs,
        booking_link=booking_link,
        seed_public_ids=seed_public_ids,
        is_freemium=is_freemium,
        action_fraction=action_fraction,
    )


@mcp.tool()
def delete_campaign(campaign_id: int) -> dict:
    """Hard-delete a Campaign and cascade to its Deals. Use with care."""
    return _safe_call(get_client().delete_campaign, campaign_id)


@mcp.tool()
def activate_campaign(campaign_id: int) -> dict:
    """Atomically activate this campaign and deactivate any other active campaign for the same account.

    Returns: ``{"campaign_id", "active": true, "deactivated_campaign_id"}``
    The worker bound to this account hot-swaps within seconds — no restart.
    """
    return _safe_call(get_client().activate_campaign, campaign_id)


@mcp.tool()
def deactivate_campaign(campaign_id: int) -> dict:
    """Mark a campaign inactive without activating another. The worker idles afterwards."""
    return _safe_call(get_client().deactivate_campaign, campaign_id)


@mcp.tool()
def list_campaign_deals(
    campaign_id: int,
    state: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List Deals for a campaign, paginated.

    Args:
        state: Optional ProfileState filter — one of "Qualified", "Ready to Connect",
               "Pending", "Connected", "Completed", "Failed".
        limit: Page size (default 100).
        offset: Offset into the result set (default 0).

    Returns: ``{"campaign_id", "total", "limit", "offset", "deals": [...]}``
    """
    return _safe_call(
        get_client().list_campaign_deals,
        campaign_id,
        state=state,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def get_campaign_stats(campaign_id: int) -> dict:
    """Counts of Deals grouped by ProfileState for a campaign.

    Returns: ``{"campaign_id", "stats": {"Qualified": N, "Connected": N, ...}}``
    """
    return _safe_call(get_client().get_campaign_stats, campaign_id)


# ── Leads & deals ───────────────────────────────────────────────────────────


@mcp.tool()
def get_lead(public_id: str) -> dict:
    """Fetch a Lead by its public_identifier (the slug in the LinkedIn URL).

    Cache-only read — does NOT trigger a Voyager API fetch and never spends
    rate-limit budget.
    """
    return _safe_call(get_client().get_lead, public_id)


@mcp.tool()
def list_lead_deals(public_id: str) -> dict:
    """List every Deal across all campaigns for a single Lead."""
    return _safe_call(get_client().list_lead_deals, public_id)


@mcp.tool()
def get_deal(deal_id: int) -> dict:
    """Fetch a single Deal by integer id, including state and closing_reason."""
    return _safe_call(get_client().get_deal, deal_id)


# ── Messages & tasks ────────────────────────────────────────────────────────


@mcp.tool()
def send_message(campaign_id: int, public_id: str, message: str) -> dict:
    """Enqueue a LinkedIn message for delivery.

    Args:
        campaign_id: Active campaign id (must be active for the lead's account worker).
        public_id: Lead public_identifier (slug).
        message: Literal message text. NOT an instruction — the worker sends this verbatim.

    Returns: ``{"task_id", "status": "pending"}`` — the send is asynchronous.
    Poll get_task(task_id) to observe completion, or subscribe to the
    ``message.sent`` / ``message.failed`` webhooks.

    The worker tries the Voyager createMessage API first (single-digit-second
    sends) and only falls back to browser navigation if the API call fails.
    """
    return _safe_call(get_client().send_message, campaign_id, public_id, message)


@mcp.tool()
def get_conversation(public_id: str) -> dict:
    """Read the synced ChatMessage history for a Lead from the database.

    Returns: ``{"public_id", "messages": [{"sender", "text", "timestamp", "is_outgoing"}, ...]}``
    """
    return _safe_call(get_client().get_conversation, public_id)


@mcp.tool()
def get_task(task_id: int) -> dict:
    """Poll a Task's status (used to observe send_message completion).

    Returns: ``{"task_id", "task_type", "status": "pending"|"running"|"completed"|"failed"|"cancelled",
                "created_at", "started_at", "completed_at", "error"}``
    """
    return _safe_call(get_client().get_task, task_id)
