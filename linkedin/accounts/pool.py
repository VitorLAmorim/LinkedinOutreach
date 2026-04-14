# linkedin/accounts/pool.py
"""Worker pool coordination for LinkedInAccounts.

A worker container claims an eligible account (active, has an active campaign,
not claimed by anyone else or claim is stale) via an atomic SELECT FOR UPDATE
SKIP LOCKED query. It then heartbeats on every loop iteration and releases the
claim on graceful shutdown or when its account stops being eligible.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# A claim is considered stale if no heartbeat for this many seconds.
# The daemon loop heartbeats every _IDLE_POLL_INTERVAL (5s), so 60s is 12x.
STALE_CLAIM_TIMEOUT = 60


def claim_next_account(worker_id: str):
    """Atomically claim an eligible account for this worker.

    Eligible: not archived, active, has at least one active campaign, and
    either unclaimed, stale, or already claimed by this worker. Returns the
    claimed LinkedInAccount or None if no eligible row is available.
    """
    from linkedin.models import Campaign, LinkedInAccount

    stale_cutoff = timezone.now() - timedelta(seconds=STALE_CLAIM_TIMEOUT)
    has_active_campaign = Campaign.objects.filter(account=OuterRef("pk"), active=True)

    with transaction.atomic():
        qs = (
            LinkedInAccount.objects
            .select_for_update(skip_locked=True)
            .annotate(_has_campaign=Exists(has_active_campaign))
            .filter(_has_campaign=True)
            .filter(is_archived=False, active=True)
            .filter(
                Q(claimed_by="")
                | Q(last_heartbeat__lt=stale_cutoff)
                | Q(claimed_by=worker_id)
            )
            .order_by("pk")
        )
        account = qs.first()
        if account is None:
            return None

        account.claimed_by = worker_id
        account.claimed_at = timezone.now()
        account.last_heartbeat = timezone.now()
        account.save(update_fields=["claimed_by", "claimed_at", "last_heartbeat"])

    logger.info("Claimed account=%s as worker=%s", account.username, worker_id)
    return account


def release_account(account, worker_id: str) -> bool:
    """Release an account iff still claimed by this worker. Returns True on release."""
    from linkedin.models import LinkedInAccount

    updated = (
        LinkedInAccount.objects
        .filter(pk=account.pk, claimed_by=worker_id)
        .update(claimed_by="", claimed_at=None, last_heartbeat=None)
    )
    if updated:
        logger.info("Released account=%s from worker=%s", account.username, worker_id)
        return True
    logger.debug(
        "release_account: no-op for account=%s worker=%s (already released or stolen)",
        account.username, worker_id,
    )
    return False


def heartbeat(account, worker_id: str) -> bool:
    """Update last_heartbeat iff we still own the claim. Returns True if still owned."""
    from linkedin.models import LinkedInAccount

    updated = (
        LinkedInAccount.objects
        .filter(pk=account.pk, claimed_by=worker_id)
        .update(last_heartbeat=timezone.now())
    )
    return bool(updated)


def is_still_eligible(account) -> bool:
    """Check whether an account still qualifies for pool work.

    Returns False if the account was archived/deactivated externally, or if
    its active campaign was deactivated with no replacement.
    """
    from linkedin.models import Campaign, LinkedInAccount

    fresh = LinkedInAccount.objects.filter(pk=account.pk).first()
    if fresh is None or fresh.is_archived or not fresh.active:
        return False
    return Campaign.objects.filter(account_id=account.pk, active=True).exists()
