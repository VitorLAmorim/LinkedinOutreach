# linkedin/tasks/send_message.py
"""Send-message task — sends a LinkedIn message on behalf of the API caller."""
from __future__ import annotations

import logging

from termcolor import colored

logger = logging.getLogger(__name__)


def handle_send_message(task, session, qualifiers):
    from crm.models import Lead
    from linkedin.actions.message import send_raw_message
    from linkedin.db.chat import sync_conversation
    from linkedin.db.deals import get_profile_dict_for_public_id
    from linkedin.exceptions import (
        AuthenticationError, LeadNotFoundError, MessageSendAmbiguous,
    )
    from linkedin.webhooks import fire_webhook

    payload = task.payload
    public_id = payload["public_id"]
    message = payload["message"]
    campaign_id = payload.get("campaign_id")

    logger.info(
        "[%s] %s %s",
        session.campaign, colored("\u25b6 send_message", "blue", attrs=["bold"]), public_id,
    )

    profile_dict = get_profile_dict_for_public_id(session, public_id)
    if profile_dict is None:
        lead = Lead.objects.filter(public_identifier=public_id).first()
        if not lead:
            fire_webhook("message.failed", {
                "public_id": public_id,
                "campaign_id": campaign_id,
                "message": message,
                "error": "Lead not found",
            })
            raise LeadNotFoundError(f"send_message: no Lead for {public_id}")
        profile_dict = lead.to_profile_dict()

    profile = profile_dict.get("profile") or profile_dict

    # Ensure URN is cached so the Voyager API path can run without browser navigation.
    if not profile.get("urn"):
        lead = Lead.objects.filter(public_identifier=public_id).first()
        if lead:
            try:
                profile["urn"] = lead.get_urn(session)
            except (AuthenticationError, ValueError) as e:
                logger.warning("send_message: could not resolve URN for %s — %s", public_id, e)

    try:
        sent = send_raw_message(session, profile, message)
    except MessageSendAmbiguous as e:
        fire_webhook("message.failed", {
            "public_id": public_id,
            "campaign_id": campaign_id,
            "message": message,
            "error": f"ambiguous — do not retry: {e}",
        })
        raise

    if sent:
        sync_conversation(session, public_id)
        fire_webhook("message.sent", {
            "public_id": public_id,
            "campaign_id": campaign_id,
            "message": message,
        })
        return

    fire_webhook("message.failed", {
        "public_id": public_id,
        "campaign_id": campaign_id,
        "message": message,
        "error": "send_raw_message returned False",
    })
    raise RuntimeError(f"send_raw_message returned False for {public_id}")


def enqueue_send_message(campaign_id: int, public_id: str, message: str) -> int:
    """Enqueue a SEND_MESSAGE task and return its ID."""
    from django.utils import timezone
    from linkedin.models import Task

    task = Task.objects.create(
        task_type=Task.TaskType.SEND_MESSAGE,
        scheduled_at=timezone.now(),
        payload={
            "campaign_id": campaign_id,
            "public_id": public_id,
            "message": message,
        },
    )
    return task.pk
