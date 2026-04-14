# tests/conftest.py
from unittest.mock import patch

import numpy as np
import pytest

from linkedin.management.setup_crm import setup_crm


@pytest.fixture(autouse=True)
def _ensure_crm_data(db):
    """
    Ensure CRM bootstrap data exists before every test.
    Uses `db` fixture (not transactional_db) for compatibility.
    Since transaction=True tests rollback, we re-create data each time.
    """
    setup_crm()


@pytest.fixture(autouse=True)
def _mock_embeddings(request):
    """Stub fastembed so tests don't need the ONNX model."""
    if "no_embed_mock" in request.keywords:
        yield
    else:
        with patch("linkedin.ml.embeddings.embed_text", return_value=np.ones(384)):
            yield


class FakeAccountSession:
    """Minimal stand-in for AccountSession — exposes account + campaign."""

    def __init__(self, account, campaign):
        self.account = account
        self.campaign = campaign

    @property
    def campaigns(self):
        from linkedin.models import Campaign
        return Campaign.objects.filter(account=self.account)

    def invalidate_campaigns_cache(self):
        pass

    def ensure_browser(self):
        pass


@pytest.fixture
def fake_session(db):
    """An AccountSession-like object backed by the Django test DB."""
    from linkedin.models import Campaign, LinkedInAccount

    account, _ = LinkedInAccount.objects.get_or_create(
        username="testuser",
        defaults={
            "linkedin_username": "testuser@example.com",
            "linkedin_password": "testpass",
        },
    )

    campaign = Campaign.objects.filter(account=account).first()
    if campaign is None:
        campaign = Campaign.objects.create(name="LinkedIn Outreach", account=account)

    return FakeAccountSession(account=account, campaign=campaign)
