# tests/tasks/test_send_message.py
from datetime import timedelta

import pytest
from unittest.mock import patch, MagicMock

from django.utils import timezone

from linkedin.models import Task
from linkedin.tasks.send_message import handle_send_message, enqueue_send_message


SAMPLE_PROFILE = {
    "first_name": "Alice",
    "last_name": "Smith",
    "headline": "Engineer",
    "positions": [{"company_name": "Acme"}],
    "urn": "urn:li:fsd_profile:ACoAAAAAAAA",
}


def _make_lead(public_id="alice"):
    from linkedin.db.leads import create_enriched_lead
    url = f"https://www.linkedin.com/in/{public_id}/"
    create_enriched_lead(MagicMock(), url, SAMPLE_PROFILE)


def _make_task(payload):
    return Task.objects.create(
        task_type=Task.TaskType.SEND_MESSAGE,
        status=Task.Status.RUNNING,
        scheduled_at=timezone.now(),
        started_at=timezone.now(),
        payload=payload,
    )


@pytest.mark.django_db
class TestHandleSendMessage:
    @patch("linkedin.webhooks.fire_webhook")
    @patch("linkedin.db.chat.sync_conversation")
    @patch("linkedin.actions.message.send_raw_message", return_value=True)
    def test_sends_and_syncs(self, mock_send, mock_sync, mock_webhook, fake_session):
        _make_lead()
        task = _make_task({
            "campaign_id": fake_session.campaign.pk,
            "public_id": "alice",
            "message": "Hello Alice!",
        })
        handle_send_message(task, fake_session, {})

        mock_send.assert_called_once()
        assert mock_send.call_args[0][1]["first_name"] == "Alice"
        assert mock_send.call_args[0][2] == "Hello Alice!"
        mock_sync.assert_called_once_with(fake_session, "alice")
        mock_webhook.assert_called_once_with("message.sent", {
            "public_id": "alice",
            "campaign_id": fake_session.campaign.pk,
            "message": "Hello Alice!",
        })

    @patch("linkedin.webhooks.fire_webhook")
    @patch("linkedin.actions.message.send_raw_message", return_value=False)
    def test_send_failure_fires_failed_webhook_and_raises(self, mock_send, mock_webhook, fake_session):
        _make_lead()
        task = _make_task({
            "campaign_id": fake_session.campaign.pk,
            "public_id": "alice",
            "message": "Hi!",
        })
        with pytest.raises(RuntimeError, match="returned False"):
            handle_send_message(task, fake_session, {})

        mock_webhook.assert_called_once()
        assert mock_webhook.call_args[0][0] == "message.failed"

    @patch("linkedin.webhooks.fire_webhook")
    def test_missing_lead_fires_failed_webhook_and_raises(self, mock_webhook, fake_session):
        from linkedin.exceptions import LeadNotFoundError

        task = _make_task({
            "campaign_id": fake_session.campaign.pk,
            "public_id": "nonexistent",
            "message": "Hi!",
        })
        with pytest.raises(LeadNotFoundError):
            handle_send_message(task, fake_session, {})

        mock_webhook.assert_called_once()
        assert mock_webhook.call_args[0][0] == "message.failed"
        assert "not found" in mock_webhook.call_args[0][1]["error"]

    @patch("linkedin.webhooks.fire_webhook")
    @patch("linkedin.actions.message.send_raw_message")
    def test_ambiguous_api_failure_fires_failed_webhook_and_raises(
        self, mock_send, mock_webhook, fake_session,
    ):
        from linkedin.exceptions import MessageSendAmbiguous

        mock_send.side_effect = MessageSendAmbiguous("Voyager 500 exhausted")
        _make_lead()
        task = _make_task({
            "campaign_id": fake_session.campaign.pk,
            "public_id": "alice",
            "message": "Hi!",
        })
        with pytest.raises(MessageSendAmbiguous):
            handle_send_message(task, fake_session, {})

        mock_webhook.assert_called_once()
        assert mock_webhook.call_args[0][0] == "message.failed"
        assert "ambiguous" in mock_webhook.call_args[0][1]["error"]


@pytest.mark.django_db
class TestEnqueueSendMessage:
    def test_creates_task(self, fake_session):
        task_id = enqueue_send_message(fake_session.campaign.pk, "alice", "Hello!")

        task = Task.objects.get(pk=task_id)
        assert task.task_type == Task.TaskType.SEND_MESSAGE
        assert task.status == Task.Status.PENDING
        assert task.payload["public_id"] == "alice"
        assert task.payload["message"] == "Hello!"

    def test_allows_duplicates(self, fake_session):
        id1 = enqueue_send_message(fake_session.campaign.pk, "alice", "First")
        id2 = enqueue_send_message(fake_session.campaign.pk, "alice", "Second")
        assert id1 != id2

    def test_send_message_has_priority_over_older_tasks(self, fake_session):
        now = timezone.now()
        Task.objects.create(
            task_type=Task.TaskType.CONNECT,
            scheduled_at=now - timedelta(seconds=5),
            payload={"campaign_id": fake_session.campaign.pk},
        )
        Task.objects.create(
            task_type=Task.TaskType.FOLLOW_UP,
            scheduled_at=now - timedelta(seconds=10),
            payload={"campaign_id": fake_session.campaign.pk},
        )
        send_id = enqueue_send_message(fake_session.campaign.pk, "alice", "urgent")

        next_task = Task.objects.claim_next()
        assert next_task.pk == send_id
        assert next_task.task_type == Task.TaskType.SEND_MESSAGE
