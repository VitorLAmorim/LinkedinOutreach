import logging
import signal
import sys

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Launch browser with the account's proxy + saved cookies for free VNC use (no automation, no timeouts)."

    def add_arguments(self, parser):
        parser.add_argument("username", help="LinkedInAccount username")

    def handle(self, *args, **options):
        from linkedin.browser.login import launch_browser, LINKEDIN_FEED_URL, LINKEDIN_LOGIN_URL
        from linkedin.browser.registry import resolve_account, get_or_create_session

        username = options["username"]
        account = resolve_account(username)
        if account is None:
            logger.error("No LinkedInAccount found for username '%s'.", username)
            sys.exit(1)

        session = get_or_create_session(account)

        account.refresh_from_db(fields=["cookie_data", "proxy_url"])
        storage_state = account.cookie_data or None

        page, context, browser, playwright, proxy_url = launch_browser(
            storage_state=storage_state, account=account
        )
        session.page = page
        session.context = context
        session.browser = browser
        session.playwright = playwright
        session._launched_with_proxy = proxy_url

        target = LINKEDIN_FEED_URL if storage_state else LINKEDIN_LOGIN_URL
        try:
            page.goto(target)
        except Exception as e:
            logger.warning("Initial navigation to %s failed (%s) — browser is still open for manual use.", target, e)

        logger.info(
            "Browse mode active for %s. Use VNC to interact with the browser. "
            "Ctrl+C or docker stop to exit.",
            username,
        )

        def _shutdown(signum, frame):
            logger.info("Received signal %s — closing browser.", signum)
            try:
                session.close()
            except Exception:
                logger.debug("session.close() raised during shutdown", exc_info=True)
            sys.exit(0)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
        signal.pause()
