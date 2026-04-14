# tests/api/test_api_auth.py
"""401 coverage for every API endpoint group.

Every endpoint decorated with ``@require_api_key`` in ``linkedin/api_views.py``
should be verified to reject unauthenticated requests with a 401 (or 503 when
``API_KEY`` is unset). A smoke test per endpoint group protects against
accidental decorator removal during refactors.
"""
from __future__ import annotations

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db


PROTECTED_ENDPOINTS = [
    ("GET", "/api/accounts/"),
    ("POST", "/api/accounts/"),
    ("GET", "/api/accounts/1/"),
    ("GET", "/api/campaigns/"),
    ("POST", "/api/campaigns/"),
    ("GET", "/api/campaigns/1/"),
    ("POST", "/api/campaigns/1/activate/"),
    ("POST", "/api/campaigns/1/deactivate/"),
    ("GET", "/api/campaigns/1/deals/"),
    ("GET", "/api/campaigns/1/stats/"),
    ("GET", "/api/leads/alice/"),
    ("GET", "/api/leads/alice/deals/"),
    ("GET", "/api/deals/1/"),
    ("POST", "/api/messages/send/"),
    ("GET", "/api/messages/alice/"),
    ("GET", "/api/tasks/1/"),
]


def _request(method, path, headers=None):
    client = Client()
    fn = getattr(client, method.lower())
    return fn(path, content_type="application/json", **(headers or {}))


@pytest.mark.parametrize("method,path", PROTECTED_ENDPOINTS)
def test_endpoint_requires_auth(settings, method, path):
    settings.DEBUG = False
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("linkedin.api_views.API_KEY", "secret-key")
        resp = _request(method, path)
    assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}"


@pytest.mark.parametrize("method,path", PROTECTED_ENDPOINTS)
def test_endpoint_503_when_api_key_unset(method, path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("linkedin.api_views.API_KEY", "")
        resp = _request(method, path)
    assert resp.status_code == 503, f"{method} {path} returned {resp.status_code}"


@pytest.mark.parametrize("method,path", PROTECTED_ENDPOINTS)
def test_endpoint_rejects_wrong_bearer(method, path):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("linkedin.api_views.API_KEY", "secret-key")
        resp = _request(
            method, path,
            headers={"HTTP_AUTHORIZATION": "Bearer wrong"},
        )
    assert resp.status_code == 401
