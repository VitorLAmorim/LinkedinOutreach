# linkedin/daemon.py
from __future__ import annotations

import logging
import random
import time
import traceback
from datetime import timedelta
from zoneinfo import ZoneInfo

import openai
from django.utils import timezone

from termcolor import colored

from linkedin.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    ACTIVE_TIMEZONE,
    CAMPAIGN_CONFIG,
    ENABLE_ACTIVE_HOURS,
    REST_DAYS,
)
from linkedin.diagnostics import failure_diagnostics
from linkedin.exceptions import AuthenticationError
from linkedin.ml.qualifier import BayesianQualifier, KitQualifier
from linkedin.models import Task
from linkedin.tasks.check_inbox import handle_check_inbox
from linkedin.tasks.check_pending import handle_check_pending
from linkedin.tasks.connect import enqueue_check_pending, enqueue_connect, enqueue_follow_up, handle_connect
from linkedin.tasks.follow_up import handle_follow_up
from linkedin.tasks.send_message import handle_send_message

logger = logging.getLogger(__name__)

# How often (seconds) the idle daemon polls the DB for a newly activated campaign.
_IDLE_POLL_INTERVAL = 5

_HANDLERS = {
    Task.TaskType.CONNECT: handle_connect,
    Task.TaskType.CHECK_PENDING: handle_check_pending,
    Task.TaskType.FOLLOW_UP: handle_follow_up,
    Task.TaskType.SEND_MESSAGE: handle_send_message,
    Task.TaskType.CHECK_INBOX: handle_check_inbox,
}


class _FreemiumRotator:
    """Logs rotating freemium messages every *every* task executions."""

    _MESSAGES = [
        colored("Join the community or give direct feedback on Telegram \u2192 https://t.me/+Y5bh9Vg8UVg5ODU0", "blue",
                attrs=["bold"]),
        "\033[38;5;208;1mLove OpenOutreach? Sponsor the project \u2192 https://github.com/sponsors/eracle\033[0m",
    ]

    def __init__(self, every: int = 10):
        self._every = every
        self._ticks = 0
        self._next = 0

    def maybe_log(self):
        self._ticks += 1
        if self._ticks % self._every == 0:
            logger.info(self._MESSAGES[self._next % len(self._MESSAGES)])
            self._next += 1


def _build_qualifiers(campaigns, cfg, kit_model=None):
    """Create a qualifier for every campaign, keyed by campaign PK."""
    from crm.models import Lead

    qualifiers: dict[int, BayesianQualifier | KitQualifier] = {}
    for campaign in campaigns:
        if campaign.is_freemium:
            if kit_model is None:
                continue
            qualifiers[campaign.pk] = KitQualifier(kit_model)
        else:
            q = BayesianQualifier(
                seed=42,
                n_mc_samples=cfg["qualification_n_mc_samples"],
                campaign=campaign,
            )
            X, y = Lead.get_labeled_arrays(campaign)
            if len(X) > 0:
                q.warm_start(X, y)
                logger.info(
                    colored("GP qualifier warm-started", "cyan")
                    + " on %d labelled samples (%d positive, %d negative)"
                    + " for campaign %s",
                    len(y), int((y == 1).sum()), int((y == 0).sum()), campaign,
                )
            qualifiers[campaign.pk] = q

    return qualifiers


# ------------------------------------------------------------------
# Active-hours schedule guard
# ------------------------------------------------------------------


def seconds_until_active() -> float:
    """Return seconds to wait before the next active window, or 0 if active now."""
    if not ENABLE_ACTIVE_HOURS:
        return 0.0
    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)

    if now.weekday() not in REST_DAYS and ACTIVE_START_HOUR <= now.hour < ACTIVE_END_HOUR:
        return 0.0

    candidate = timezone.make_aware(
        now.replace(hour=ACTIVE_START_HOUR, minute=0, second=0, microsecond=0, tzinfo=None),
        timezone=tz,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() in REST_DAYS:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds()


def remaining_active_seconds() -> float:
    """Seconds remaining in the current active window.

    Used by spread-delay logic to distribute connection requests evenly.
    Returns 0 if outside the active window (shouldn't happen — daemon sleeps).
    When active hours are disabled, returns min(seconds to midnight, default window).
    """
    if not ENABLE_ACTIVE_HOURS:
        default_hours = CAMPAIGN_CONFIG["default_spread_window_hours"]
        now = timezone.now()
        midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        return min((midnight - now).total_seconds(), default_hours * 3600.0)

    tz = ZoneInfo(ACTIVE_TIMEZONE)
    now = timezone.localtime(timezone=tz)

    if now.weekday() in REST_DAYS or now.hour >= ACTIVE_END_HOUR or now.hour < ACTIVE_START_HOUR:
        return 0.0

    end_today = now.replace(hour=ACTIVE_END_HOUR, minute=0, second=0, microsecond=0)
    return (end_today - now).total_seconds()


# ------------------------------------------------------------------
# Active-campaign tracking (one campaign per account at any time)
# ------------------------------------------------------------------


def get_active_campaign_id(account) -> int | None:
    """Return the PK of the currently active campaign for the account, or None."""
    from linkedin.models import Campaign
    return Campaign.objects.filter(account=account, active=True).values_list("pk", flat=True).first()


def cancel_pending_tasks_for_campaign(campaign_id: int) -> int:
    """Cancel all pending tasks for a deactivated campaign."""
    return Task.objects.filter(
        status=Task.Status.PENDING,
        payload__campaign_id=campaign_id,
    ).update(status=Task.Status.CANCELLED, completed_at=timezone.now())


# ------------------------------------------------------------------
# Task queue worker
# ------------------------------------------------------------------


def heal_tasks(session):
    """Reconcile task queue with CRM state on daemon startup or campaign swap.

    1. Reset stale 'running' tasks to 'pending' (crashed worker recovery)
    2. Seed one 'connect' task per campaign if none pending
    3. Create 'check_pending' tasks for PENDING profiles without tasks
    4. Create 'follow_up' tasks for CONNECTED profiles without tasks
    5. Seed check_inbox tasks per campaign if webhook is configured
    """
    from crm.models import Deal
    from linkedin.url_utils import url_to_public_id
    from linkedin.enums import ProfileState

    cfg = CAMPAIGN_CONFIG
    campaign_ids = [c.pk for c in session.campaigns]
    if not campaign_ids:
        logger.info("heal_tasks: no active campaigns for %s", session.account.username)
        return

    stale_count = Task.objects.filter(
        status=Task.Status.RUNNING,
        payload__campaign_id__in=campaign_ids,
    ).update(
        status=Task.Status.PENDING,
    )
    if stale_count:
        logger.info("Recovered %d stale running tasks", stale_count)

    for campaign in session.campaigns:
        delay = CAMPAIGN_CONFIG["connect_delay_seconds"] if campaign.is_freemium else 0
        enqueue_connect(campaign.pk, delay_seconds=delay)

    for campaign in session.campaigns:
        session.campaign = campaign
        pending_deals = Deal.objects.filter(
            state=ProfileState.PENDING,
            campaign=campaign,
        ).select_related("lead")

        for deal in pending_deals:
            public_id = url_to_public_id(deal.lead.linkedin_url) if deal.lead.linkedin_url else None
            if not public_id:
                continue
            backoff = deal.backoff_hours or cfg["check_pending_recheck_after_hours"]
            enqueue_check_pending(campaign.pk, public_id, backoff_hours=backoff)

    for campaign in session.campaigns:
        session.campaign = campaign
        connected_deals = Deal.objects.filter(
            state=ProfileState.CONNECTED,
            campaign=campaign,
        ).select_related("lead")

        for deal in connected_deals:
            public_id = url_to_public_id(deal.lead.linkedin_url) if deal.lead.linkedin_url else None
            if not public_id:
                continue
            enqueue_follow_up(campaign.pk, public_id, delay_seconds=random.uniform(5, 60))

    from linkedin.conf import WEBHOOK_URL
    if WEBHOOK_URL:
        from linkedin.tasks.check_inbox import enqueue_check_inbox
        for campaign in session.campaigns:
            enqueue_check_inbox(campaign.pk)

    own_tasks = Task.objects.filter(payload__campaign_id__in=campaign_ids)
    pending_count = own_tasks.pending().count()
    logger.info("Task queue healed: %d pending tasks", pending_count)


class _PlaceholderAccount:
    """Stand-in for when pool mode has no eligible account yet."""
    pk = None
    username = "<pool-idle>"
    linkedin_username = "<pool-idle>"
    is_archived = True
    active = False
    proxy_url = ""
    cookie_data = None


def _refresh_account_binding(session, worker_id: str) -> bool:
    """Pool-mode hot-swap check. Returns True if the session swapped accounts.

    Heartbeat the current claim. If our account stopped being eligible
    (archived, deactivated, or lost its active campaign), release the claim,
    tear down the browser, claim a new account, and relaunch. If no eligible
    account is available, the session is left with no browser and the caller
    should short-circuit to an idle sleep.
    """
    if not worker_id:
        return False

    from linkedin.accounts.pool import (
        claim_next_account, heartbeat, is_still_eligible, release_account,
    )

    owned = heartbeat(session.account, worker_id)
    if owned and is_still_eligible(session.account):
        return False

    if not owned:
        logger.warning(
            colored("Claim lost", "yellow", attrs=["bold"])
            + " for account=%s — someone else stole it. Releasing and rebinding.",
            session.account.username,
        )
    else:
        logger.info(
            colored("Releasing account", "yellow", attrs=["bold"])
            + "=%s — no longer eligible (campaign deactivated or account archived)",
            session.account.username,
        )
        release_account(session.account, worker_id)

    session.close()

    new_account = claim_next_account(worker_id)
    if new_account is None:
        logger.info("No eligible account to claim — idling")
        session.account = _PlaceholderAccount()  # daemon will treat as no-op
        return True

    session.swap_account(new_account)
    session.ensure_browser()
    return True


def _refresh_proxy(session) -> bool:
    """Detect a runtime proxy change on the current account and restart the browser.

    Returns True if the browser was restarted. No-op if proxy is unchanged,
    browser is not currently launched, or the session is holding the idle
    placeholder account (which has no DB row).
    """
    if session.page is None:
        return False
    if isinstance(session.account, _PlaceholderAccount):
        return False

    from linkedin.browser.login import _resolve_proxy_url

    session.account.refresh_from_db(fields=["proxy_url"])
    current = _resolve_proxy_url(session.account)
    if current == (session._launched_with_proxy or ""):
        return False

    logger.info(
        colored("Proxy changed", "yellow", attrs=["bold"])
        + " for %s — restarting browser", session.account.username,
    )
    session.close()
    session.ensure_browser()
    return True


def _refresh_active_campaign(session, last_active_id, qualifiers, kit_model):
    """Detect a campaign-activation change for this account and react.

    Returns the (possibly new) active campaign id and a flag indicating
    whether qualifiers were rebuilt.
    """
    current_id = get_active_campaign_id(session.account)
    if current_id == last_active_id:
        return last_active_id, qualifiers

    if last_active_id is not None:
        cancelled = cancel_pending_tasks_for_campaign(last_active_id)
        logger.info(
            colored("Campaign deactivated", "yellow", attrs=["bold"])
            + " — cancelled %d pending tasks for campaign id=%s",
            cancelled, last_active_id,
        )

    session.invalidate_campaigns_cache()

    if current_id is None:
        session.campaign = None
        logger.info("No active campaign for %s — idling", session.account.username)
        return None, {}

    if not session.campaigns:
        # get_active_campaign_id saw an active row but the M2M query came
        # back empty — race window between activate/deactivate on different
        # threads. Treat as idle and re-check on the next loop iteration.
        session.campaign = None
        logger.info(
            "No campaigns resolved for %s despite current_id=%s — will retry",
            session.account.username, current_id,
        )
        return None, {}

    qualifiers = _build_qualifiers(session.campaigns, CAMPAIGN_CONFIG, kit_model=kit_model)
    session.campaign = session.campaigns[0]
    heal_tasks(session)
    logger.info(
        colored("Campaign activated", "green", attrs=["bold"])
        + " — %s (id=%s) for %s",
        session.campaign, current_id, session.account.username,
    )
    return current_id, qualifiers


def run_daemon(session, worker_id: str = ""):
    from linkedin.ml.hub import fetch_kit
    from linkedin.setup.freemium import import_freemium_campaign, seed_profiles
    from linkedin.models import Campaign

    cfg = CAMPAIGN_CONFIG

    session.bind_worker_id(worker_id)

    kit = fetch_kit()
    kit_model = kit["model"] if kit else None
    if kit and not isinstance(session.account, _PlaceholderAccount):
        freemium_campaign = import_freemium_campaign(session.account, kit["config"])
        if freemium_campaign:
            prev_campaign = session.campaign
            session.campaign = freemium_campaign
            seed_profiles(session, kit["config"])
            session.campaign = prev_campaign

    session.invalidate_campaigns_cache()
    qualifiers = _build_qualifiers(session.campaigns, cfg, kit_model=kit_model)

    last_active_id = get_active_campaign_id(session.account)
    if last_active_id is not None:
        heal_tasks(session)

    logger.info(
        colored("Daemon started", "green", attrs=["bold"])
        + " — account=%s, %d active campaigns, worker_id=%s",
        session.account.username, len(session.campaigns), worker_id or "<pinned>",
    )

    freemium = _FreemiumRotator(every=2)

    while True:
        pause = seconds_until_active()
        if pause > 0:
            h, m = int(pause // 3600), int(pause % 3600 // 60)
            logger.info("Outside active hours — sleeping %dh%02dm", h, m)
            time.sleep(pause)
            continue

        # Pool-mode hot-swap: check whether our claimed account is still eligible.
        if _refresh_account_binding(session, worker_id):
            last_active_id = None  # force _refresh_active_campaign to reseed
            qualifiers = _build_qualifiers(session.campaigns, cfg, kit_model=kit_model)
            if isinstance(session.account, _PlaceholderAccount):
                time.sleep(_IDLE_POLL_INTERVAL)
                continue

        # Per-account proxy hot-swap: restart browser if proxy_url changed.
        _refresh_proxy(session)

        last_active_id, qualifiers = _refresh_active_campaign(
            session, last_active_id, qualifiers, kit_model,
        )

        if last_active_id is None:
            time.sleep(_IDLE_POLL_INTERVAL)
            continue

        campaign_ids = [c.pk for c in session.campaigns]
        own_tasks = Task.objects.filter(payload__campaign_id__in=campaign_ids)

        task = own_tasks.claim_next()
        if task is None:
            wait = own_tasks.seconds_to_next()
            if wait is None:
                time.sleep(_IDLE_POLL_INTERVAL)
                continue
            sleep_for = min(wait, _IDLE_POLL_INTERVAL) if wait > 0 else 0
            if sleep_for > 0:
                time.sleep(sleep_for)
            continue

        campaign = Campaign.objects.filter(pk=task.payload.get("campaign_id")).first()
        if not campaign or not campaign.active:
            task.mark_cancelled()
            continue

        session.campaign = campaign
        task.mark_running()

        handler = _HANDLERS.get(task.task_type)
        if handler is None:
            task.mark_failed(f"Unknown task type: {task.task_type}")
            continue

        try:
            with failure_diagnostics(session):
                handler(task, session, qualifiers)
        except AuthenticationError:
            logger.warning("Session expired during %s — re-authenticating", task)
            try:
                session.reauthenticate()
            except Exception:
                task.mark_failed(traceback.format_exc())
                logger.exception("Re-authentication failed for %s", task)
                continue
            task.reset_to_pending()
            continue
        except (openai.BadRequestError, openai.AuthenticationError, openai.NotFoundError) as e:
            task.mark_failed(str(e))
            logger.error(
                colored("Daemon stopped — OpenAI API error", "red", attrs=["bold"])
                + "\n%s\nCheck ai_model, llm_api_key, and llm_api_base in Admin → Site Configuration.", e,
            )
            return
        except Exception:
            task.mark_failed(traceback.format_exc())
            logger.exception("Task %s failed", task)
            continue

        task.mark_completed()
        freemium.maybe_log()
