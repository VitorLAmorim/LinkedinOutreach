# linkedin/tasks/check_inbox.py
"""Check-inbox task — polls recent LinkedIn conversations and fires webhooks for new messages.

Cursor semantics are at-least-once: the next cursor is snapshotted *before*
the Voyager fetch, and the persisted ``Campaign.last_inbox_check_at`` is only
advanced after every webhook in the batch is delivered without raising. If
any webhook raises, the cursor is left untouched and the next run re-fires
the whole batch — duplicates are the webhook consumer's problem.

Self-reschedule runs in a ``finally`` block so a single failed run does not
break the polling loop. The handler's own exception still propagates, so the
failed task surfaces in monitoring.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_tz

from termcolor import colored

logger = logging.getLogger(__name__)


def _participant_urns(conv: dict) -> list[str]:
    """Extract hostIdentityUrn values from conversation participants."""
    urns = []
    for p in conv.get("conversationParticipants", []):
        urn = p.get("hostIdentityUrn")
        if urn:
            urns.append(urn)
    return urns


def _find_lead_for_urns(urns: list[str], self_urn: str):
    """Match participant URNs to a Lead in the DB. Returns Lead or None."""
    from crm.models import Lead

    for urn in urns:
        if urn == self_urn:
            continue
        lead = Lead.objects.filter(profile_data__urn=urn).first()
        if lead:
            return lead
    return None


def handle_check_inbox(task, session, qualifiers):
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType
    from django.utils import timezone

    from linkedin.api.client import PlaywrightLinkedinAPI
    from linkedin.api.messaging import fetch_conversations
    from linkedin.conf import CHECK_INBOX_INTERVAL_SECONDS
    from linkedin.db.chat import sync_conversation
    from linkedin.models import Campaign
    from linkedin.webhooks import fire_webhook

    payload = task.payload
    campaign_id = payload["campaign_id"]
    interval = payload.get("interval_seconds", CHECK_INBOX_INTERVAL_SECONDS)

    logger.info(
        "[%s] %s",
        session.campaign, colored("\u25b6 check_inbox", "magenta", attrs=["bold"]),
    )

    try:
        campaign = Campaign.objects.get(pk=campaign_id)

        # Bootstrap: first run drops historical inbox on purpose so operators
        # aren't flooded. Flip inbox_bootstrap_complete in admin to backfill.
        if not campaign.inbox_bootstrap_complete:
            logger.info(
                "check_inbox: bootstrap — historical inbox skipped for campaign=%s",
                campaign_id,
            )
            Campaign.objects.filter(pk=campaign_id).update(
                last_inbox_check_at=timezone.now(),
                inbox_bootstrap_complete=True,
            )
            return

        # Snapshot the cursor BEFORE the fetch so any message that arrives
        # during the fetch window is re-delivered on the next run.
        next_cursor = timezone.now()
        last_checked = campaign.last_inbox_check_at or next_cursor

        session.ensure_browser()
        api = PlaywrightLinkedinAPI(session=session)
        mailbox_urn = session.self_profile["urn"]

        raw = fetch_conversations(api, mailbox_urn)
        elements = (
            raw.get("data", {})
            .get("messengerConversationsBySyncToken", {})
            .get("elements", [])
        )

        new_msg_count = 0
        for conv in elements:
            last_activity_ms = conv.get("lastActivityAt")
            if last_activity_ms:
                last_activity = datetime.fromtimestamp(last_activity_ms / 1000, tz=dt_tz.utc)
                if last_activity <= last_checked:
                    continue

            participant_urns = _participant_urns(conv)
            lead = _find_lead_for_urns(participant_urns, mailbox_urn)
            if not lead:
                continue

            sync_conversation(session, lead.public_identifier)

            ct = ContentType.objects.get_for_model(lead)
            new_messages = ChatMessage.objects.filter(
                content_type=ct,
                object_id=lead.pk,
                is_outgoing=False,
                creation_date__gt=last_checked,
            ).order_by("creation_date")

            for msg in new_messages:
                new_msg_count += 1
                fire_webhook("message.received", {
                    "public_id": lead.public_identifier,
                    "campaign_id": campaign_id,
                    "sender_name": f"{lead.first_name or ''} {lead.last_name or ''}".strip(),
                    "message": msg.content,
                    "linkedin_urn": msg.linkedin_urn or "",
                    "received_at": msg.creation_date.isoformat() if msg.creation_date else "",
                })

        if new_msg_count:
            logger.info("check_inbox: %d new incoming messages", new_msg_count)

        # Advance the persisted cursor only after every webhook fan-out
        # returned without raising. fire_webhook swallows delivery errors
        # internally but re-raises on invariant violations (bad URL, etc.).
        Campaign.objects.filter(pk=campaign_id).update(last_inbox_check_at=next_cursor)
    finally:
        try:
            enqueue_check_inbox(campaign_id, interval)
        except Exception:
            logger.exception("check_inbox: failed to self-reschedule for %s", campaign_id)


def enqueue_check_inbox(campaign_id: int, interval_seconds: int = 300):
    from linkedin.conf import CHECK_INBOX_INTERVAL_SECONDS
    from linkedin.tasks.connect import _enqueue_task
    from linkedin.models import Task

    _enqueue_task(
        task_type=Task.TaskType.CHECK_INBOX,
        payload={
            "campaign_id": campaign_id,
            "interval_seconds": interval_seconds or CHECK_INBOX_INTERVAL_SECONDS,
        },
        delay_seconds=interval_seconds or CHECK_INBOX_INTERVAL_SECONDS,
        dedup_keys=["campaign_id"],
    )
