import logging
import sys

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Interactive VNC login for a LinkedIn account. Polls for auth cookie, saves session."

    def add_arguments(self, parser):
        parser.add_argument("username", help="LinkedInAccount username")

    def handle(self, *args, **options):
        from linkedin.browser.login import interactive_setup
        from linkedin.browser.registry import resolve_account, get_or_create_session

        account = resolve_account(options["username"])
        if account is None:
            logger.error("No LinkedInAccount found for username '%s'.", options["username"])
            sys.exit(1)

        session = get_or_create_session(account)
        interactive_setup(session)
        logger.info("Setup complete for %s. Restart in normal mode to run the daemon.", options["username"])
