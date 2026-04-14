# linkedin/browser/registry.py
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from linkedin.browser.session import AccountSession

logger = logging.getLogger(__name__)

_sessions: dict[int, AccountSession] = {}


def get_or_create_session(account) -> AccountSession:
    from linkedin.browser.session import AccountSession

    pk = account.pk
    if pk not in _sessions:
        _sessions[pk] = AccountSession(account)
        logger.debug("Created new account session for %s", account)
    return _sessions[pk]


def get_first_active_account():
    """Return the first active LinkedInAccount, or None."""
    from linkedin.models import LinkedInAccount

    return LinkedInAccount.objects.filter(active=True, is_archived=False).first()


def resolve_account(username: str | None = None):
    """Resolve a LinkedInAccount from an optional username, falling back to first active."""
    if username:
        from linkedin.models import LinkedInAccount

        return LinkedInAccount.objects.filter(username=username).first()
    return get_first_active_account()


def cli_parser(description: str):
    """Bootstrap Django and return an ArgumentParser with ``--handle``.

    Call from ``if __name__ == "__main__"`` blocks. Sets up Django,
    configures logging, and returns a parser with ``--handle`` pre-added.
    After adding extra arguments, call ``cli_session(args)`` to get the session.
    """
    import argparse
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")

    import django
    django.setup()

    logging.basicConfig(level=logging.DEBUG, format="[%(levelname)s] %(message)s")
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--handle", default=None, help="LinkedInAccount username (default: first active account)")
    return parser


def cli_session(args) -> AccountSession:
    """Resolve account from parsed args, create session, set default campaign."""
    account = resolve_account(args.handle)
    if not account:
        print("No active LinkedInAccount found.")
        raise SystemExit(1)

    session = get_or_create_session(account)
    if not session.campaigns:
        print(f"No campaigns found for {account}.")
        raise SystemExit(1)
    session.campaign = session.campaigns[0]
    return session
