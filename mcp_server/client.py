"""Sync HTTP client for the OpenOutreach REST API.

One method per REST endpoint defined in linkedin/api_views.py + linkedin/urls.py.
All methods raise OpenOutreachAPIError on non-2xx responses; otherwise return
the parsed JSON body as a dict (or list, for collection endpoints).

Used by mcp_server/server.py — keep this layer free of MCP / FastMCP imports
so it can be unit-tested standalone with httpx.MockTransport.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class OpenOutreachAPIError(RuntimeError):
    """Raised when the OpenOutreach REST API returns a non-2xx response."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"OpenOutreach API error {status_code}: {body}")


class OpenOutreachClient:
    """Thin sync httpx wrapper. One instance per process."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = (base_url or os.getenv("OPENOUTREACH_BASE_URL", "http://localhost:8000")).rstrip("/")
        self.api_key = api_key or os.getenv("OPENOUTREACH_API_KEY", "")
        self.timeout = timeout if timeout is not None else float(os.getenv("OPENOUTREACH_TIMEOUT", "30"))

        if not self.api_key:
            raise RuntimeError(
                "OPENOUTREACH_API_KEY is required (Bearer token for the REST API)",
            )

        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={"Authorization": f"Bearer {self.api_key}"},
            transport=transport,
        )

    # -- core request helper -------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        resp = self._http.request(method, path, json=json, params=params)
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code >= 400:
            raise OpenOutreachAPIError(resp.status_code, body)
        return body

    def close(self) -> None:
        self._http.close()

    # ── Accounts ──────────────────────────────────────────────────────────

    def list_accounts(self) -> list[dict]:
        return self._request("GET", "/api/accounts/")["accounts"]

    def create_account(
        self,
        username: str,
        linkedin_username: str,
        linkedin_password: str,
        *,
        connect_daily_limit: int | None = None,
        connect_weekly_limit: int | None = None,
        follow_up_daily_limit: int | None = None,
        subscribe_newsletter: bool | None = None,
        legal_accepted: bool | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "username": username,
            "linkedin_username": linkedin_username,
            "linkedin_password": linkedin_password,
        }
        for key, value in (
            ("connect_daily_limit", connect_daily_limit),
            ("connect_weekly_limit", connect_weekly_limit),
            ("follow_up_daily_limit", follow_up_daily_limit),
            ("subscribe_newsletter", subscribe_newsletter),
            ("legal_accepted", legal_accepted),
        ):
            if value is not None:
                body[key] = value
        return self._request("POST", "/api/accounts/", json=body)

    def get_account(self, account_id: int) -> dict:
        return self._request("GET", f"/api/accounts/{account_id}/")

    def update_account(
        self,
        account_id: int,
        *,
        linkedin_username: str | None = None,
        linkedin_password: str | None = None,
        subscribe_newsletter: bool | None = None,
        connect_daily_limit: int | None = None,
        connect_weekly_limit: int | None = None,
        follow_up_daily_limit: int | None = None,
        active: bool | None = None,
        legal_accepted: bool | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        for key, value in (
            ("linkedin_username", linkedin_username),
            ("linkedin_password", linkedin_password),
            ("subscribe_newsletter", subscribe_newsletter),
            ("connect_daily_limit", connect_daily_limit),
            ("connect_weekly_limit", connect_weekly_limit),
            ("follow_up_daily_limit", follow_up_daily_limit),
            ("active", active),
            ("legal_accepted", legal_accepted),
        ):
            if value is not None:
                body[key] = value
        return self._request("PATCH", f"/api/accounts/{account_id}/", json=body)

    def archive_account(self, account_id: int) -> dict:
        return self._request("DELETE", f"/api/accounts/{account_id}/")

    # ── Campaigns ─────────────────────────────────────────────────────────

    def list_campaigns(
        self,
        *,
        account_id: int | None = None,
        active: bool | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {}
        if account_id is not None:
            params["account_id"] = account_id
        if active is not None:
            params["active"] = "true" if active else "false"
        return self._request("GET", "/api/campaigns/", params=params)["campaigns"]

    def create_campaign(
        self,
        name: str,
        account_id: int,
        *,
        campaign_objective: str | None = None,
        product_docs: str | None = None,
        booking_link: str | None = None,
        seed_public_ids: list[str] | None = None,
        is_freemium: bool | None = None,
        action_fraction: float | None = None,
    ) -> dict:
        body: dict[str, Any] = {"name": name, "account_id": account_id}
        for key, value in (
            ("campaign_objective", campaign_objective),
            ("product_docs", product_docs),
            ("booking_link", booking_link),
            ("seed_public_ids", seed_public_ids),
            ("is_freemium", is_freemium),
            ("action_fraction", action_fraction),
        ):
            if value is not None:
                body[key] = value
        return self._request("POST", "/api/campaigns/", json=body)

    def get_campaign(self, campaign_id: int) -> dict:
        return self._request("GET", f"/api/campaigns/{campaign_id}/")

    def update_campaign(
        self,
        campaign_id: int,
        *,
        name: str | None = None,
        campaign_objective: str | None = None,
        product_docs: str | None = None,
        booking_link: str | None = None,
        seed_public_ids: list[str] | None = None,
        is_freemium: bool | None = None,
        action_fraction: float | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        for key, value in (
            ("name", name),
            ("campaign_objective", campaign_objective),
            ("product_docs", product_docs),
            ("booking_link", booking_link),
            ("seed_public_ids", seed_public_ids),
            ("is_freemium", is_freemium),
            ("action_fraction", action_fraction),
        ):
            if value is not None:
                body[key] = value
        return self._request("PATCH", f"/api/campaigns/{campaign_id}/", json=body)

    def delete_campaign(self, campaign_id: int) -> dict:
        return self._request("DELETE", f"/api/campaigns/{campaign_id}/")

    def activate_campaign(self, campaign_id: int) -> dict:
        return self._request("POST", f"/api/campaigns/{campaign_id}/activate/")

    def deactivate_campaign(self, campaign_id: int) -> dict:
        return self._request("POST", f"/api/campaigns/{campaign_id}/deactivate/")

    def list_campaign_deals(
        self,
        campaign_id: int,
        *,
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if state is not None:
            params["state"] = state
        return self._request("GET", f"/api/campaigns/{campaign_id}/deals/", params=params)

    def get_campaign_stats(self, campaign_id: int) -> dict:
        return self._request("GET", f"/api/campaigns/{campaign_id}/stats/")

    # ── Leads & deals ─────────────────────────────────────────────────────

    def get_lead(self, public_id: str) -> dict:
        return self._request("GET", f"/api/leads/{public_id}/")

    def list_lead_deals(self, public_id: str) -> dict:
        return self._request("GET", f"/api/leads/{public_id}/deals/")

    def get_deal(self, deal_id: int) -> dict:
        return self._request("GET", f"/api/deals/{deal_id}/")

    # ── Messages & tasks ──────────────────────────────────────────────────

    def send_message(self, campaign_id: int, public_id: str, message: str) -> dict:
        return self._request(
            "POST",
            "/api/messages/send/",
            json={
                "campaign_id": campaign_id,
                "public_id": public_id,
                "message": message,
            },
        )

    def get_conversation(self, public_id: str) -> dict:
        return self._request("GET", f"/api/messages/{public_id}/")

    def get_task(self, task_id: int) -> dict:
        return self._request("GET", f"/api/tasks/{task_id}/")
