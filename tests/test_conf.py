# tests/test_conf.py
import pytest

from linkedin.browser.registry import get_first_active_account


@pytest.mark.django_db
class TestGetFirstActiveAccount:
    def test_returns_account_when_exists(self, fake_session):
        result = get_first_active_account()
        assert result is not None
        assert result.username == "testuser"

    def test_returns_none_when_no_accounts(self, db):
        from linkedin.models import LinkedInAccount
        LinkedInAccount.objects.all().delete()
        assert get_first_active_account() is None

    def test_returns_none_when_all_inactive(self, fake_session):
        from linkedin.models import LinkedInAccount
        LinkedInAccount.objects.all().update(active=False)
        assert get_first_active_account() is None
