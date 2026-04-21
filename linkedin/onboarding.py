# linkedin/onboarding.py
"""Onboarding: create Campaign + LinkedInAccount records.

LLM config (``LLM_API_KEY``, ``AI_MODEL``, ``LLM_API_BASE``) lives in `.env`
and is read at process start via ``linkedin.conf.get_llm_config``. The
onboarding wizard no longer collects or writes LLM values.

Two ways to supply config:
- OnboardConfig.from_json(path) — from a JSON file (non-interactive / cloud).
- collect_from_wizard()         — interactive questionary wizard (needs TTY).

Both return an OnboardConfig; ``apply()`` is the single write path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from pathlib import Path

from linkedin.conf import (
    DEFAULT_CONNECT_DAILY_LIMIT,
    DEFAULT_CONNECT_WEEKLY_LIMIT,
    DEFAULT_FOLLOW_UP_DAILY_LIMIT,
    ROOT_DIR,
)

DEFAULT_PRODUCT_DOCS = ROOT_DIR / "README.md"
DEFAULT_CAMPAIGN_OBJECTIVE = ROOT_DIR / "docs" / "default_campaign.md"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config dataclass (pure data — no I/O)
# ---------------------------------------------------------------------------

@dataclass
class OnboardConfig:
    """All values needed to onboard — filled interactively or from JSON.

    LLM config lives in `.env`, not here — see ``linkedin.conf.LLM_API_KEY``.
    """

    linkedin_email: str = ""
    linkedin_password: str = ""
    campaign_name: str = ""
    product_description: str = ""
    campaign_objective: str = ""
    booking_link: str = ""
    seed_urls: str = ""
    newsletter: bool = True
    connect_daily_limit: int = DEFAULT_CONNECT_DAILY_LIMIT
    connect_weekly_limit: int = DEFAULT_CONNECT_WEEKLY_LIMIT
    follow_up_daily_limit: int = DEFAULT_FOLLOW_UP_DAILY_LIMIT
    legal_acceptance: bool = False

    @classmethod
    def from_json(cls, path) -> "OnboardConfig":
        """Load config from a JSON file, ignoring unknown keys."""
        import json
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})


# ---------------------------------------------------------------------------
# State inspection
# ---------------------------------------------------------------------------

_CAMPAIGN_KEYS = {
    "campaign_name", "product_description", "campaign_objective",
    "booking_link", "seed_urls",
}
_ACCOUNT_KEYS = {
    "linkedin_email", "linkedin_password", "newsletter",
    "connect_daily_limit", "connect_weekly_limit", "follow_up_daily_limit",
    "legal_acceptance",
}
_ALL_KEYS = _CAMPAIGN_KEYS | _ACCOUNT_KEYS


def missing_keys() -> set[str]:
    """Return onboarding field keys that still need values.

    LLM config is not reported here — it lives in `.env` and is validated
    by ``rundaemon`` directly before the task loop starts.
    """
    from linkedin.models import Campaign, LinkedInAccount

    keys: set[str] = set()

    if not Campaign.objects.exists():
        keys |= _CAMPAIGN_KEYS

    if not LinkedInAccount.objects.filter(active=True).exists():
        keys |= _ACCOUNT_KEYS

    return keys


# ---------------------------------------------------------------------------
# Interactive collection (needs TTY)
# ---------------------------------------------------------------------------

def collect_from_wizard() -> OnboardConfig:
    """Run the questionary wizard for missing fields; return an OnboardConfig.

    Raises SystemExit if the user cancels.
    """
    from openoutreach.prompts import SELF_HOSTED_QUESTIONS
    from openoutreach.wizard import ask

    # LLM config has moved to `.env`; skip any LLM questions the upstream
    # prompt set still ships with so the wizard doesn't ask the operator
    # for values that would be ignored anyway.
    _WIZARD_SKIP = (_ALL_KEYS - missing_keys()) | {"llm_api_key", "ai_model", "llm_api_base"}
    questions = [q for q in SELF_HOSTED_QUESTIONS if q.key not in _WIZARD_SKIP]
    if not questions or not any(q.required for q in questions):
        return OnboardConfig()

    answers = ask(questions)
    if answers is None:
        raise SystemExit("Onboarding cancelled.")

    valid = {f.name for f in fields(OnboardConfig)}
    return OnboardConfig(**{k: v for k, v in answers.items() if k in valid})


# ---------------------------------------------------------------------------
# Record creation (pure DB, no I/O)
# ---------------------------------------------------------------------------

def _read_default_file(path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _email_to_handle(email: str) -> str:
    return email.split("@")[0].lower().replace(".", "_").replace("+", "_")


def _create_account(
    email: str,
    password: str,
    *,
    subscribe: bool = True,
    connect_daily: int = DEFAULT_CONNECT_DAILY_LIMIT,
    connect_weekly: int = DEFAULT_CONNECT_WEEKLY_LIMIT,
    follow_up_daily: int = DEFAULT_FOLLOW_UP_DAILY_LIMIT,
):
    """Create a LinkedInAccount record and return it."""
    from linkedin.models import LinkedInAccount

    handle = _email_to_handle(email)
    account = LinkedInAccount.objects.create(
        username=handle,
        linkedin_username=email,
        linkedin_password=password,
        subscribe_newsletter=subscribe,
        connect_daily_limit=connect_daily,
        connect_weekly_limit=connect_weekly,
        follow_up_daily_limit=follow_up_daily,
    )
    logger.info("Created LinkedIn account for %s (handle=%s)", email, handle)
    print(f"Account '{handle}' created!")
    return account


def _create_campaign(
    account,
    name: str,
    product_docs: str,
    objective: str,
    booking_link: str = "",
):
    """Create a Campaign bound to an account and return it."""
    from linkedin.models import Campaign

    campaign = Campaign.objects.create(
        name=name,
        account=account,
        product_docs=product_docs,
        campaign_objective=objective,
        booking_link=booking_link,
    )
    logger.info("Created campaign: %s for account %s", name, account.username)
    print(f"Campaign '{name}' created!")
    return campaign


def _create_seed_leads(campaign, seed_urls: str) -> None:
    """Parse seed URL text and create QUALIFIED leads."""
    if not seed_urls or not seed_urls.strip():
        return
    from linkedin.setup.seeds import parse_seed_urls, create_seed_leads

    public_ids = parse_seed_urls(seed_urls)
    if public_ids:
        created = create_seed_leads(campaign, public_ids)
        print(f"{created} seed profile(s) added as QUALIFIED.")


# ---------------------------------------------------------------------------
# Single write path
# ---------------------------------------------------------------------------

def apply(config: OnboardConfig) -> None:
    """Idempotent: create missing Account, Campaign, env vars, and legal acceptance."""
    from linkedin.management.setup_crm import DEFAULT_CAMPAIGN_NAME
    from linkedin.models import Campaign, LinkedInAccount

    # Account first — campaigns now require an account FK
    account = LinkedInAccount.objects.filter(active=True).first()
    if account is None and config.linkedin_email:
        account = _create_account(
            config.linkedin_email,
            config.linkedin_password,
            subscribe=config.newsletter,
            connect_daily=config.connect_daily_limit,
            connect_weekly=config.connect_weekly_limit,
            follow_up_daily=config.follow_up_daily_limit,
        )

    # Campaign
    if account is not None and not Campaign.objects.filter(account=account).exists() and config.campaign_name:
        campaign = _create_campaign(
            account,
            name=config.campaign_name or DEFAULT_CAMPAIGN_NAME,
            product_docs=config.product_description or _read_default_file(DEFAULT_PRODUCT_DOCS),
            objective=config.campaign_objective or _read_default_file(DEFAULT_CAMPAIGN_OBJECTIVE),
            booking_link=config.booking_link,
        )
        _create_seed_leads(campaign, config.seed_urls)

    # LLM config is read from env vars on process start (see linkedin.conf).

    # Legal
    if config.legal_acceptance:
        LinkedInAccount.objects.filter(legal_accepted=False, active=True).update(legal_accepted=True)
