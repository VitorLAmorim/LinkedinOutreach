import logging
import os
import socket
import sys
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


def _derive_worker_id() -> str:
    """Stable identifier for this worker process (container hostname + pid)."""
    host = os.environ.get("HOSTNAME") or socket.gethostname()
    return f"{host}-{os.getpid()}"


class Command(BaseCommand):
    help = "Run the OpenOutreach daemon (onboard, validate, start task queue)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--profile",
            default=None,
            help=(
                "LinkedInAccount username to pin this worker to. "
                "Leave empty (or unset LINKEDIN_PROFILE) to run in pool mode, "
                "where the worker dynamically claims any eligible account."
            ),
        )

    def handle(self, *args, **options):
        self._configure_logging()
        self._ensure_db()
        self._ensure_onboarded()

        profile = options.get("profile") or ""
        worker_id = _derive_worker_id()

        if profile:
            session = self._create_session(account_username=profile)
        else:
            logger.info("Entering pool mode (worker_id=%s) — waiting for claim", worker_id)
            session = self._create_pool_session(worker_id)

        from linkedin.browser.session import install_shutdown_handler
        install_shutdown_handler(session)

        self._ensure_newsletter(session)

        from linkedin.daemon import run_daemon
        run_daemon(session, worker_id=worker_id if not profile else "")

    # -- Steps ---------------------------------------------------------------

    def _configure_logging(self):
        logging.getLogger().handlers.clear()
        logging.basicConfig(level=logging.DEBUG, format="%(message)s")
        for name in (
            "urllib3", "httpx", "langchain", "openai", "playwright",
            "httpcore", "fastembed", "huggingface_hub", "filelock", "asyncio",
        ):
            logging.getLogger(name).setLevel(logging.WARNING)

    def _ensure_db(self):
        call_command("migrate", "--no-input")

        from linkedin.management.setup_crm import setup_crm
        setup_crm()

    def _ensure_onboarded(self):
        from linkedin.onboarding import apply, collect_from_wizard, missing_keys

        if not missing_keys():
            return

        if sys.stdin.isatty():
            apply(collect_from_wizard())
        else:
            missing = missing_keys()
            self.stderr.write(
                f"Onboarding incomplete and no TTY available.\n"
                f"Missing: {', '.join(sorted(missing))}\n"
                f"Run with an interactive terminal to complete onboarding."
            )
            sys.exit(1)

    def _create_pool_session(self, worker_id: str):
        """Block until an eligible account is claimable, then build a session for it.

        Installs a minimal SIGTERM/SIGINT handler *before* entering the claim
        loop so that a signal arriving between a successful claim and the
        full ``install_shutdown_handler`` call in ``handle()`` still releases
        the claim cleanly. The handler is replaced by the session-aware one
        later — ``signal.signal`` overwrites prior handlers.
        """
        import signal

        from linkedin.accounts.pool import claim_next_account, release_account
        from linkedin.browser.registry import get_or_create_session
        from linkedin.conf import get_llm_config

        llm_api_key, _, _ = get_llm_config()
        if not llm_api_key:
            logger.error("LLM_API_KEY is required. Set it in Site Configuration (Django Admin).")
            sys.exit(1)

        claim_holder: dict = {"account": None}

        def _early_handler(signum, frame):
            logger.warning("Received signal %s during pool startup — releasing", signum)
            if claim_holder["account"] is not None:
                try:
                    release_account(claim_holder["account"], worker_id)
                except Exception:
                    logger.exception("Failed to release claim on early shutdown")
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _early_handler)
        signal.signal(signal.SIGINT, _early_handler)

        while True:
            account = claim_next_account(worker_id)
            if account is not None:
                claim_holder["account"] = account
                break
            logger.info("No eligible account — retrying in 5m")
            time.sleep(300)

        session = get_or_create_session(account)
        if session.campaigns:
            campaign = next(
                (c for c in session.campaigns if not c.is_freemium), None,
            ) or session.campaigns[0]
            session.campaign = campaign
        return session

    def _create_session(self, account_username=None):
        from linkedin.browser.registry import (
            get_first_active_account, get_or_create_session, resolve_account,
        )
        from linkedin.conf import get_llm_config

        llm_api_key, _, _ = get_llm_config()
        if not llm_api_key:
            logger.error("LLM_API_KEY is required. Set it in Site Configuration (Django Admin).")
            sys.exit(1)

        account = resolve_account(account_username) if account_username else get_first_active_account()
        if account is None:
            logger.error("No active LinkedIn accounts found.")
            sys.exit(1)

        session = get_or_create_session(account)

        if not session.campaigns:
            logger.warning("No active campaigns for %s — daemon will idle until one is activated.", account.username)
            session.campaign = None
            return session

        campaign = next(
            (c for c in session.campaigns if not c.is_freemium), None,
        ) or session.campaigns[0]
        session.campaign = campaign

        return session

    def _ensure_newsletter(self, session):
        if not getattr(session.account, "pk", None):
            return  # placeholder account in pool mode — daemon will retry after claim
        if session.account.newsletter_processed:
            return

        from linkedin.api.newsletter import ensure_newsletter_subscription
        from linkedin.setup.gdpr import apply_gdpr_newsletter_override
        from linkedin.url_utils import public_id_to_url

        profile = session.self_profile
        country_code = profile.get("country_code")
        apply_gdpr_newsletter_override(session, country_code)
        linkedin_url = public_id_to_url(profile["public_identifier"])
        ensure_newsletter_subscription(session, linkedin_url=linkedin_url)
        session.account.newsletter_processed = True
        session.account.save(update_fields=["newsletter_processed"])
