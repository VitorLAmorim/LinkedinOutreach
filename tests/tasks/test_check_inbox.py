# tests/tasks/test_check_inbox.py
import pytest
from datetime import datetime, timezone as dt_tz, timedelta
from unittest.mock import patch, MagicMock

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from chat.models import ChatMessage
from crm.models import Lead
from linkedin.models import Campaign, Task
from linkedin.tasks.check_inbox import handle_check_inbox, enqueue_check_inbox


def _bootstrap(campaign, cursor=None):
    """Mark the campaign's inbox bootstrap complete and optionally set cursor."""
    campaign.inbox_bootstrap_complete = True
    if cursor is not None:
        campaign.last_inbox_check_at = cursor
    campaign.save(update_fields=["inbox_bootstrap_complete", "last_inbox_check_at"])


SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "urn": "urn:li:fsd_profile:ALICE123",
    "positions": [{"company_name": "Acme"}],
}

SELF_URN = "urn:li:fsd_profile:SELF456"


def _make_lead(public_id="alice"):
    from linkedin.db.leads import create_enriched_lead
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(MagicMock(), url, SAMPLE_PROFILE)
    return Lead.objects.get(public_identifier=public_id)


def _make_task(payload):
    return Task.objects.create(
        task_type=Task.TaskType.CHECK_INBOX,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload=payload,
    )


def _fake_conversations(last_activity_ms, participant_urn):
    return {
        "data": {
            "messengerConversationsBySyncToken": {
                "elements": [{
                    "entityUrn": "urn:li:msg_conversation:CONV1",
                    "lastActivityAt": last_activity_ms,
                    "conversationParticipants": [
                        {"hostIdentityUrn": participant_urn},
                        {"hostIdentityUrn": SELF_URN},
                    ],
                }],
            },
        },
    }


@pytest.mark.django_db
class TestHandleCheckInbox:
    @patch("linkedin.webhooks.fire_webhook")
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.api.messaging.fetch_conversations")
    @patch("linkedin.api.client.PlaywrightLinkedinAPI")
    def test_fires_webhook_for_new_messages(self, mock_api_cls, mock_fetch, mock_sync, mock_webhook, fake_session):
        lead = _make_lead()
        _bootstrap(fake_session.campaign, cursor=timezone.now() - timedelta(minutes=5))

        # Set up: a conversation with recent activity
        now_ms = int(timezone.now().timestamp() * 1000)
        mock_fetch.return_value = _fake_conversations(now_ms, SAMPLE_PROFILE["urn"])

        # Create a new incoming message in DB (simulating what sync_conversation would do)
        ct = ContentType.objects.get_for_model(lead)
        ChatMessage.objects.create(
            content_type=ct,
            object_id=lead.pk,
            content="Hey there!",
            is_outgoing=False,
            linkedin_urn="urn:li:msg_message:MSG1",
            creation_date=timezone.now(),
        )

        task = _make_task({
            "campaign_id": fake_session.campaign.pk,
            "interval_seconds": 300,
        })

        fake_session.self_profile = {"urn": SELF_URN}

        handle_check_inbox(task, fake_session, {})

        mock_webhook.assert_called_once()
        event, data = mock_webhook.call_args[0]
        assert event == "message.received"
        assert data["public_id"] == "alice"
        assert data["message"] == "Hey there!"

    @patch("linkedin.webhooks.fire_webhook")
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.api.messaging.fetch_conversations")
    @patch("linkedin.api.client.PlaywrightLinkedinAPI")
    def test_skips_old_conversations(self, mock_api_cls, mock_fetch, mock_sync, mock_webhook, fake_session):
        _make_lead()
        _bootstrap(fake_session.campaign, cursor=timezone.now())

        # Conversation activity is older than the persisted cursor
        old_ms = int((timezone.now() - timedelta(hours=1)).timestamp() * 1000)
        mock_fetch.return_value = _fake_conversations(old_ms, SAMPLE_PROFILE["urn"])

        task = _make_task({
            "campaign_id": fake_session.campaign.pk,
            "interval_seconds": 300,
        })

        fake_session.self_profile = {"urn": SELF_URN}

        handle_check_inbox(task, fake_session, {})

        mock_sync.assert_not_called()
        mock_webhook.assert_not_called()

    @patch("linkedin.webhooks.fire_webhook")
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.api.messaging.fetch_conversations")
    @patch("linkedin.api.client.PlaywrightLinkedinAPI")
    def test_skips_unknown_participants(self, mock_api_cls, mock_fetch, mock_sync, mock_webhook, fake_session):
        _bootstrap(fake_session.campaign, cursor=timezone.now() - timedelta(minutes=5))

        # No lead in DB matching the participant URN
        now_ms = int(timezone.now().timestamp() * 1000)
        mock_fetch.return_value = _fake_conversations(now_ms, "urn:li:fsd_profile:UNKNOWN")

        task = _make_task({
            "campaign_id": fake_session.campaign.pk,
            "interval_seconds": 300,
        })

        fake_session.self_profile = {"urn": SELF_URN}

        handle_check_inbox(task, fake_session, {})

        mock_sync.assert_not_called()
        mock_webhook.assert_not_called()

    @patch("linkedin.webhooks.fire_webhook")
    @patch("linkedin.api.messaging.fetch_conversations")
    @patch("linkedin.api.client.PlaywrightLinkedinAPI")
    def test_bootstrap_skips_historical_and_sets_flag(self, mock_api_cls, mock_fetch, mock_webhook, fake_session):
        assert fake_session.campaign.inbox_bootstrap_complete is False

        task = _make_task({
            "campaign_id": fake_session.campaign.pk,
            "interval_seconds": 300,
        })
        fake_session.self_profile = {"urn": SELF_URN}

        handle_check_inbox(task, fake_session, {})

        mock_fetch.assert_not_called()
        mock_webhook.assert_not_called()

        fake_session.campaign.refresh_from_db()
        assert fake_session.campaign.inbox_bootstrap_complete is True
        assert fake_session.campaign.last_inbox_check_at is not None

    @patch("linkedin.webhooks.fire_webhook")
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.api.messaging.fetch_conversations")
    @patch("linkedin.api.client.PlaywrightLinkedinAPI")
    def test_cursor_advances_after_successful_run(self, mock_api_cls, mock_fetch, mock_sync, mock_webhook, fake_session):
        _make_lead()
        old_cursor = timezone.now() - timedelta(hours=1)
        _bootstrap(fake_session.campaign, cursor=old_cursor)

        old_ms = int((timezone.now() - timedelta(minutes=30)).timestamp() * 1000)
        mock_fetch.return_value = _fake_conversations(old_ms, SAMPLE_PROFILE["urn"])

        task = _make_task({
            "campaign_id": fake_session.campaign.pk,
            "interval_seconds": 300,
        })
        fake_session.self_profile = {"urn": SELF_URN}

        handle_check_inbox(task, fake_session, {})

        fake_session.campaign.refresh_from_db()
        assert fake_session.campaign.last_inbox_check_at > old_cursor

    @patch("linkedin.webhooks.fire_webhook", side_effect=RuntimeError("webhook invariant broke"))
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.api.messaging.fetch_conversations")
    @patch("linkedin.api.client.PlaywrightLinkedinAPI")
    def test_cursor_not_advanced_when_webhook_raises(self, mock_api_cls, mock_fetch, mock_sync, mock_webhook, fake_session):
        lead = _make_lead()
        old_cursor = timezone.now() - timedelta(hours=1)
        _bootstrap(fake_session.campaign, cursor=old_cursor)

        now_ms = int(timezone.now().timestamp() * 1000)
        mock_fetch.return_value = _fake_conversations(now_ms, SAMPLE_PROFILE["urn"])

        ct = ContentType.objects.get_for_model(lead)
        ChatMessage.objects.create(
            content_type=ct,
            object_id=lead.pk,
            content="Hey!",
            is_outgoing=False,
            linkedin_urn="urn:li:msg_message:MSG1",
            creation_date=timezone.now(),
        )

        task = _make_task({
            "campaign_id": fake_session.campaign.pk,
            "interval_seconds": 300,
        })
        fake_session.self_profile = {"urn": SELF_URN}

        with pytest.raises(RuntimeError, match="invariant"):
            handle_check_inbox(task, fake_session, {})

        # Cursor unchanged — next run re-delivers the batch (at-least-once).
        fake_session.campaign.refresh_from_db()
        assert fake_session.campaign.last_inbox_check_at == old_cursor

        # But the next task was still enqueued in the finally block.
        assert Task.objects.filter(
            task_type=Task.TaskType.CHECK_INBOX,
            status=Task.Status.PENDING,
            payload__campaign_id=fake_session.campaign.pk,
        ).exists()


@pytest.mark.django_db
class TestEnqueueCheckInbox:
    def test_creates_task(self, fake_session):
        enqueue_check_inbox(fake_session.campaign.pk, 300)

        task = Task.objects.filter(
            task_type=Task.TaskType.CHECK_INBOX,
            status=Task.Status.PENDING,
        ).first()
        assert task is not None
        assert task.payload["campaign_id"] == fake_session.campaign.pk

    def test_deduplicates_by_campaign(self, fake_session):
        enqueue_check_inbox(fake_session.campaign.pk, 300)
        enqueue_check_inbox(fake_session.campaign.pk, 300)

        count = Task.objects.filter(
            task_type=Task.TaskType.CHECK_INBOX,
            status=Task.Status.PENDING,
            payload__campaign_id=fake_session.campaign.pk,
        ).count()
        assert count == 1
