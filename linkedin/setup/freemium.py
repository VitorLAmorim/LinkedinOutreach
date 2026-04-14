"""Freemium campaign creation from kit config."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def import_freemium_campaign(account, kit_config: dict):
    """Create or update a freemium Campaign for the given LinkedInAccount.

    Returns the Campaign instance.
    """
    from linkedin.models import Campaign

    campaign_name = f"{kit_config.get('campaign_name', 'Freemium Outreach')} ({account.username})"

    campaign, _ = Campaign.objects.update_or_create(
        name=campaign_name,
        defaults={
            "account": account,
            "product_docs": kit_config["product_docs"],
            "campaign_objective": kit_config["campaign_objective"],
            "booking_link": kit_config["booking_link"],
            "is_freemium": True,
            "action_fraction": kit_config["action_fraction"],
        },
    )

    logger.info(
        "[Freemium] Campaign imported: %s (action_fraction=%.2f)",
        campaign_name, kit_config["action_fraction"],
    )
    return campaign


def seed_profiles(session, kit_config: dict):
    """Seed Lead (with embedding) + QUALIFIED Deal for profiles listed in kit config."""
    from crm.models import Lead

    from linkedin.db.deals import create_freemium_deal
    from linkedin.url_utils import public_id_to_url

    public_ids = kit_config.get("seed_profiles", [])
    if not public_ids:
        return

    for public_id in public_ids:
        url = public_id_to_url(public_id)

        lead, _ = Lead.objects.get_or_create(public_identifier=public_id, defaults={"linkedin_url": url})

        lead.get_embedding(session)
        create_freemium_deal(session, public_id)
