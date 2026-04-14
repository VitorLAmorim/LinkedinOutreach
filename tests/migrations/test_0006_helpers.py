# tests/migrations/test_0006_helpers.py
"""Unit tests for the inlined helpers in migration 0006.

Full end-to-end migration tests would require rolling back a Postgres DB to
an earlier state, which is impractical for CI. These tests exercise the
pure-function helper and the key collision-suffix logic of
``populate_account_username`` via a fake ``apps.get_model`` registry.
"""
from __future__ import annotations

import importlib
import pytest


@pytest.fixture
def migration():
    return importlib.import_module("linkedin.migrations.0006_account_refactor")


class TestEmailToHandle:
    def test_strips_domain(self, migration):
        assert migration._email_to_handle("alice@example.com") == "alice"

    def test_lowercases(self, migration):
        assert migration._email_to_handle("Alice.Smith@EXAMPLE.COM") == "alice_smith"

    def test_replaces_special_chars(self, migration):
        assert migration._email_to_handle("a+b-c.d@x.y") == "a_b_c_d"

    def test_strips_leading_and_trailing_underscores(self, migration):
        assert migration._email_to_handle(".a.@x.y") == "a"

    def test_empty_input_falls_back_to_default(self, migration):
        assert migration._email_to_handle("") == "account"
        assert migration._email_to_handle(None) == "account"  # type: ignore[arg-type]

    def test_all_special_chars_fall_back(self, migration):
        assert migration._email_to_handle("@@@@@") == "account"


class _FakeQuerySet:
    def __init__(self, rows):
        self._rows = rows

    def order_by(self, *_fields):
        return self

    def __iter__(self):
        return iter(self._rows)


class _FakeAccountRow:
    def __init__(self, pk, linkedin_username):
        self.pk = pk
        self.linkedin_username = linkedin_username
        self.username = ""
        self._saved_fields = []

    def save(self, update_fields=None):
        self._saved_fields = list(update_fields or [])


class _FakeAccountManager:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return _FakeQuerySet(self._rows)

    def order_by(self, *_fields):
        return _FakeQuerySet(self._rows)


class _FakeAccountModel:
    def __init__(self, rows):
        self.objects = _FakeAccountManager(rows)


class _FakeApps:
    def __init__(self, accounts):
        self._accounts = accounts

    def get_model(self, app, model):
        assert (app, model) == ("linkedin", "LinkedInAccount")
        return self._accounts


class TestPopulateAccountUsername:
    def test_distinct_emails_map_to_distinct_handles(self, migration):
        rows = [
            _FakeAccountRow(1, "alice@example.com"),
            _FakeAccountRow(2, "bob@example.com"),
        ]
        migration.populate_account_username(_FakeApps(_FakeAccountModel(rows)), None)
        assert rows[0].username == "alice"
        assert rows[1].username == "bob"

    def test_colliding_emails_get_numeric_suffix(self, migration):
        rows = [
            _FakeAccountRow(1, "alice@foo.com"),
            _FakeAccountRow(2, "ALICE@bar.com"),
            _FakeAccountRow(3, "alice@baz.io"),
        ]
        migration.populate_account_username(_FakeApps(_FakeAccountModel(rows)), None)
        assert rows[0].username == "alice"
        assert rows[1].username == "alice_2"
        assert rows[2].username == "alice_3"

    def test_empty_linkedin_username_falls_back_to_account_default(self, migration):
        rows = [
            _FakeAccountRow(1, ""),
            _FakeAccountRow(2, "bob@example.com"),
            _FakeAccountRow(3, ""),
        ]
        migration.populate_account_username(_FakeApps(_FakeAccountModel(rows)), None)
        assert rows[0].username == "account"
        assert rows[1].username == "bob"
        assert rows[2].username == "account_2"
