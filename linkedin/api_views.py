# linkedin/api_views.py
"""REST API endpoints for LinkedIn account & campaign management."""
from __future__ import annotations

import hmac
import json
import logging
from functools import wraps

from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from linkedin.conf import API_KEY

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth + helpers
# ---------------------------------------------------------------------------


def require_api_key(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not API_KEY:
            return JsonResponse({"error": "API not configured (set API_KEY env var)"}, status=503)
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {API_KEY}"
        if not hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
            return JsonResponse({"error": "Unauthorized"}, status=401)
        return view_func(request, *args, **kwargs)
    return wrapper


def _parse_json(request):
    try:
        return json.loads(request.body or b"{}"), None
    except (json.JSONDecodeError, ValueError):
        return None, JsonResponse({"error": "Invalid JSON"}, status=400)


_FIELD_TYPES = {
    "subscribe_newsletter": bool,
    "active": bool,
    "legal_accepted": bool,
    "is_archived": bool,
    "is_freemium": bool,
    "connect_daily_limit": int,
    "connect_weekly_limit": int,
    "follow_up_daily_limit": int,
    "action_fraction": float,
    "linkedin_username": str,
    "linkedin_password": str,
    "proxy_url": str,
    "name": str,
    "campaign_objective": str,
    "product_docs": str,
    "booking_link": str,
    "seed_public_ids": list,
}


def _apply_patch(obj, body: dict, allowed: tuple[str, ...]):
    """Copy type-validated fields from ``body`` onto ``obj``.

    Returns a JsonResponse (400) on type mismatch, or None on success. Only
    fields listed in ``allowed`` and present in ``_FIELD_TYPES`` are considered.
    """
    for field in allowed:
        if field not in body:
            continue
        expected = _FIELD_TYPES.get(field)
        value = body[field]
        if expected is not None and not isinstance(value, expected):
            return JsonResponse(
                {"error": f"{field} must be {expected.__name__}, got {type(value).__name__}"},
                status=400,
            )
        setattr(obj, field, value)
    return None


def _serialize_account(account) -> dict:
    from linkedin.models import Campaign

    active_id = (
        Campaign.objects.filter(account=account, active=True)
        .values_list("pk", flat=True)
        .first()
    )
    return {
        "id": account.pk,
        "username": account.username,
        "linkedin_username": account.linkedin_username,
        "active": account.active,
        "is_archived": account.is_archived,
        "legal_accepted": account.legal_accepted,
        "connect_daily_limit": account.connect_daily_limit,
        "connect_weekly_limit": account.connect_weekly_limit,
        "follow_up_daily_limit": account.follow_up_daily_limit,
        "subscribe_newsletter": account.subscribe_newsletter,
        "active_campaign_id": active_id,
        "proxy_url": account.proxy_url,
        "claimed_by": account.claimed_by,
        "last_heartbeat": account.last_heartbeat.isoformat() if account.last_heartbeat else None,
    }


def _campaign_stats(campaign) -> dict:
    from crm.models.deal import Deal
    from linkedin.enums import ProfileState

    counts = {state.value: 0 for state in ProfileState}
    rows = (
        Deal.objects.filter(campaign=campaign)
        .values("state")
        .annotate(n=Count("pk"))
    )
    for row in rows:
        counts[row["state"]] = row["n"]
    return counts


def _serialize_campaign(campaign, *, with_stats: bool = False) -> dict:
    data = {
        "id": campaign.pk,
        "name": campaign.name,
        "account_id": campaign.account_id,
        "campaign_objective": campaign.campaign_objective,
        "product_docs": campaign.product_docs,
        "booking_link": campaign.booking_link,
        "is_freemium": campaign.is_freemium,
        "action_fraction": campaign.action_fraction,
        "seed_public_ids": campaign.seed_public_ids,
        "active": campaign.active,
    }
    if with_stats:
        data["stats"] = _campaign_stats(campaign)
    return data


def _serialize_lead(lead) -> dict:
    return {
        "id": lead.pk,
        "public_identifier": lead.public_identifier,
        "linkedin_url": lead.linkedin_url,
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "company_name": lead.company_name,
        "disqualified": lead.disqualified,
        "profile": lead.profile_data or {},
        "creation_date": lead.creation_date.isoformat() if lead.creation_date else None,
        "update_date": lead.update_date.isoformat() if lead.update_date else None,
    }


def _serialize_deal(deal) -> dict:
    return {
        "id": deal.pk,
        "lead_id": deal.lead_id,
        "lead_public_identifier": deal.lead.public_identifier if deal.lead_id else None,
        "campaign_id": deal.campaign_id,
        "state": deal.state,
        "closing_reason": deal.closing_reason,
        "reason": deal.reason,
        "connect_attempts": deal.connect_attempts,
        "creation_date": deal.creation_date.isoformat() if deal.creation_date else None,
        "update_date": deal.update_date.isoformat() if deal.update_date else None,
    }


# ---------------------------------------------------------------------------
# Messages (existing)
# ---------------------------------------------------------------------------


@csrf_exempt
@require_api_key
@require_POST
def send_message_view(request):
    """POST /api/messages/send/ — enqueue a LinkedIn message for delivery."""
    from crm.models import Lead
    from linkedin.models import Campaign
    from linkedin.tasks.send_message import enqueue_send_message

    body, err = _parse_json(request)
    if err:
        return err

    campaign_id = body.get("campaign_id")
    public_id = body.get("public_id")
    message = body.get("message")

    if not campaign_id or not public_id or not message:
        return JsonResponse({"error": "campaign_id, public_id, and message are required"}, status=400)

    campaign = Campaign.objects.filter(pk=campaign_id, active=True).first()
    if not campaign:
        return JsonResponse({"error": f"Campaign {campaign_id} not found or inactive"}, status=404)

    if not Lead.objects.filter(public_identifier=public_id).exists():
        return JsonResponse({"error": f"Lead '{public_id}' not found"}, status=404)

    task_id = enqueue_send_message(campaign_id, public_id, message)
    return JsonResponse({"task_id": task_id, "status": "pending"}, status=202)


@csrf_exempt
@require_api_key
@require_GET
def task_status_view(request, task_id):
    """GET /api/tasks/<task_id>/ — poll task status."""
    from linkedin.models import Task

    task = Task.objects.filter(pk=task_id).first()
    if not task:
        return JsonResponse({"error": "Task not found"}, status=404)

    return JsonResponse({
        "task_id": task.pk,
        "task_type": task.task_type,
        "status": task.status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "error": task.error,
    })


@csrf_exempt
@require_api_key
@require_GET
def conversation_view(request, public_id):
    """GET /api/messages/<public_id>/ — read conversation history from DB."""
    from crm.models import Lead
    from linkedin.db.chat import _read_from_db

    if not Lead.objects.filter(public_identifier=public_id).exists():
        return JsonResponse({"error": f"Lead '{public_id}' not found"}, status=404)

    return JsonResponse({"public_id": public_id, "messages": _read_from_db(public_id)})


# ---------------------------------------------------------------------------
# LinkedIn accounts CRUD
# ---------------------------------------------------------------------------


@csrf_exempt
@require_api_key
@require_http_methods(["GET", "POST"])
def accounts_collection_view(request):
    """GET /api/accounts/ — list. POST /api/accounts/ — create."""
    from linkedin.models import LinkedInAccount

    if request.method == "GET":
        accounts = LinkedInAccount.objects.filter(is_archived=False).order_by("username")
        return JsonResponse({"accounts": [_serialize_account(a) for a in accounts]})

    body, err = _parse_json(request)
    if err:
        return err

    username = body.get("username")
    linkedin_username = body.get("linkedin_username")
    linkedin_password = body.get("linkedin_password")
    if not username or not linkedin_username or not linkedin_password:
        return JsonResponse({
            "error": "username, linkedin_username, and linkedin_password are required",
        }, status=400)

    try:
        with transaction.atomic():
            account = LinkedInAccount.objects.create(
                username=username,
                linkedin_username=linkedin_username,
                linkedin_password=linkedin_password,
                subscribe_newsletter=body.get("subscribe_newsletter", True),
                connect_daily_limit=body.get("connect_daily_limit", 20),
                connect_weekly_limit=body.get("connect_weekly_limit", 100),
                follow_up_daily_limit=body.get("follow_up_daily_limit", 30),
                legal_accepted=body.get("legal_accepted", False),
            )
    except IntegrityError:
        return JsonResponse({"error": f"username '{username}' already exists"}, status=409)

    return JsonResponse(_serialize_account(account), status=201)


@csrf_exempt
@require_api_key
@require_http_methods(["GET", "PATCH", "DELETE"])
def account_detail_view(request, account_id):
    """GET / PATCH / DELETE /api/accounts/<id>/."""
    from linkedin.models import LinkedInAccount

    account = LinkedInAccount.objects.filter(pk=account_id).first()
    if not account:
        return JsonResponse({"error": "Account not found"}, status=404)

    if request.method == "GET":
        return JsonResponse(_serialize_account(account))

    if request.method == "PATCH":
        body, err = _parse_json(request)
        if err:
            return err
        bad = _apply_patch(account, body, (
            "linkedin_username", "linkedin_password", "subscribe_newsletter",
            "connect_daily_limit", "connect_weekly_limit", "follow_up_daily_limit",
            "active", "legal_accepted", "proxy_url",
        ))
        if bad is not None:
            return bad
        account.save()
        return JsonResponse(_serialize_account(account))

    # DELETE — soft delete
    account.is_archived = True
    account.active = False
    account.save(update_fields=["is_archived", "active"])
    return JsonResponse({"id": account.pk, "is_archived": True})


# ---------------------------------------------------------------------------
# Campaigns CRUD
# ---------------------------------------------------------------------------


@csrf_exempt
@require_api_key
@require_http_methods(["GET", "POST"])
def campaigns_collection_view(request):
    """GET /api/campaigns/ — list. POST /api/campaigns/ — create."""
    from linkedin.models import Campaign, LinkedInAccount

    if request.method == "GET":
        qs = Campaign.objects.all().order_by("-pk")
        account_id = request.GET.get("account_id")
        if account_id:
            qs = qs.filter(account_id=account_id)
        active = request.GET.get("active")
        if active is not None:
            qs = qs.filter(active=active.lower() in ("1", "true", "yes"))
        return JsonResponse({"campaigns": [_serialize_campaign(c) for c in qs]})

    body, err = _parse_json(request)
    if err:
        return err

    name = body.get("name")
    account_id = body.get("account_id")
    if not name or not account_id:
        return JsonResponse({"error": "name and account_id are required"}, status=400)

    account = LinkedInAccount.objects.filter(pk=account_id).first()
    if not account:
        return JsonResponse({"error": f"Account {account_id} not found"}, status=404)

    try:
        with transaction.atomic():
            campaign = Campaign.objects.create(
                name=name,
                account=account,
                campaign_objective=body.get("campaign_objective", ""),
                product_docs=body.get("product_docs", ""),
                booking_link=body.get("booking_link", ""),
                seed_public_ids=body.get("seed_public_ids", []),
                is_freemium=body.get("is_freemium", False),
                action_fraction=body.get("action_fraction", 0.2),
                active=False,
            )
    except IntegrityError:
        return JsonResponse({"error": f"Campaign name '{name}' already exists"}, status=409)

    return JsonResponse(_serialize_campaign(campaign), status=201)


@csrf_exempt
@require_api_key
@require_http_methods(["GET", "PATCH", "DELETE"])
def campaign_detail_view(request, campaign_id):
    """GET / PATCH / DELETE /api/campaigns/<id>/."""
    from linkedin.models import Campaign

    campaign = Campaign.objects.filter(pk=campaign_id).first()
    if not campaign:
        return JsonResponse({"error": "Campaign not found"}, status=404)

    if request.method == "GET":
        return JsonResponse(_serialize_campaign(campaign, with_stats=True))

    if request.method == "PATCH":
        body, err = _parse_json(request)
        if err:
            return err
        bad = _apply_patch(campaign, body, (
            "name", "campaign_objective", "product_docs", "booking_link",
            "seed_public_ids", "is_freemium", "action_fraction",
        ))
        if bad is not None:
            return bad
        campaign.save()
        return JsonResponse(_serialize_campaign(campaign))

    if campaign.active:
        return JsonResponse(
            {"error": "deactivate the campaign before deleting it"},
            status=409,
        )
    campaign.delete()
    return JsonResponse({"id": campaign_id, "deleted": True})


@csrf_exempt
@require_api_key
@require_POST
def campaign_activate_view(request, campaign_id):
    """POST /api/campaigns/<id>/activate/ — atomically swap active campaign for the account."""
    from linkedin.models import Campaign, LinkedInAccount

    campaign = Campaign.objects.filter(pk=campaign_id).first()
    if not campaign:
        return JsonResponse({"error": "Campaign not found"}, status=404)

    try:
        with transaction.atomic():
            LinkedInAccount.objects.select_for_update().get(pk=campaign.account_id)
            previous = (
                Campaign.objects.select_for_update()
                .filter(account_id=campaign.account_id, active=True)
                .exclude(pk=campaign.pk)
                .first()
            )
            deactivated_id = None
            if previous:
                previous.active = False
                previous.save(update_fields=["active"])
                deactivated_id = previous.pk
            campaign.active = True
            campaign.save(update_fields=["active"])
    except IntegrityError:
        return JsonResponse(
            {"error": "another active campaign exists for this account"},
            status=409,
        )

    return JsonResponse({
        "campaign_id": campaign.pk,
        "active": True,
        "deactivated_campaign_id": deactivated_id,
    })


@csrf_exempt
@require_api_key
@require_POST
def campaign_deactivate_view(request, campaign_id):
    """POST /api/campaigns/<id>/deactivate/."""
    from linkedin.models import Campaign

    campaign = Campaign.objects.filter(pk=campaign_id).first()
    if not campaign:
        return JsonResponse({"error": "Campaign not found"}, status=404)

    campaign.active = False
    campaign.save(update_fields=["active"])
    return JsonResponse({"campaign_id": campaign.pk, "active": False})


@csrf_exempt
@require_api_key
@require_GET
def campaign_deals_view(request, campaign_id):
    """GET /api/campaigns/<id>/deals/ — list deals (filterable by state)."""
    from crm.models.deal import Deal
    from linkedin.models import Campaign

    if not Campaign.objects.filter(pk=campaign_id).exists():
        return JsonResponse({"error": "Campaign not found"}, status=404)

    qs = Deal.objects.filter(campaign_id=campaign_id).select_related("lead")
    state = request.GET.get("state")
    if state:
        qs = qs.filter(state=state)

    try:
        limit = int(request.GET.get("limit", 100))
        offset = int(request.GET.get("offset", 0))
    except ValueError:
        return JsonResponse({"error": "limit and offset must be integers"}, status=400)

    # TODO: offset/limit has a TOCTOU window between count() and slice() —
    # rows inserted/deleted between the two calls cause skipped or repeated
    # entries on subsequent pages. Swap for cursor-based pagination keyed on
    # pk when this endpoint gets real traffic.
    total = qs.count()
    deals = list(qs.order_by("-pk")[offset:offset + limit])
    return JsonResponse({
        "campaign_id": campaign_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "deals": [_serialize_deal(d) for d in deals],
    })


@csrf_exempt
@require_api_key
@require_GET
def campaign_stats_view(request, campaign_id):
    """GET /api/campaigns/<id>/stats/ — deal counts grouped by state."""
    from linkedin.models import Campaign

    campaign = Campaign.objects.filter(pk=campaign_id).first()
    if not campaign:
        return JsonResponse({"error": "Campaign not found"}, status=404)

    return JsonResponse({"campaign_id": campaign.pk, "stats": _campaign_stats(campaign)})


# ---------------------------------------------------------------------------
# Leads & deals (read-only)
# ---------------------------------------------------------------------------


@csrf_exempt
@require_api_key
@require_GET
def lead_detail_view(request, public_id):
    """GET /api/leads/<public_id>/ — single lead (cache-only, no Voyager fetch)."""
    from crm.models import Lead

    lead = Lead.objects.filter(public_identifier=public_id).first()
    if not lead:
        return JsonResponse({"error": "Lead not found"}, status=404)
    return JsonResponse(_serialize_lead(lead))


@csrf_exempt
@require_api_key
@require_GET
def lead_deals_view(request, public_id):
    """GET /api/leads/<public_id>/deals/ — all deals across campaigns for a lead."""
    from crm.models import Lead
    from crm.models.deal import Deal

    lead = Lead.objects.filter(public_identifier=public_id).first()
    if not lead:
        return JsonResponse({"error": "Lead not found"}, status=404)

    deals = Deal.objects.filter(lead=lead).select_related("lead")
    return JsonResponse({
        "public_id": public_id,
        "deals": [_serialize_deal(d) for d in deals],
    })


@csrf_exempt
@require_api_key
@require_GET
def deal_detail_view(request, deal_id):
    """GET /api/deals/<id>/ — single deal."""
    from crm.models.deal import Deal

    deal = Deal.objects.filter(pk=deal_id).select_related("lead").first()
    if not deal:
        return JsonResponse({"error": "Deal not found"}, status=404)
    return JsonResponse(_serialize_deal(deal))
