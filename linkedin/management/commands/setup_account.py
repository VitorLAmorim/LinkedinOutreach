import logging
import sys

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Interactive VNC login for a LinkedIn account. Polls for auth cookie, saves session."

    def add_arguments(self, parser):
        parser.add_argument("username", help="Django username of the LinkedInProfile")

    def handle(self, *args, **options):
        from linkedin.browser.login import interactive_setup
        from linkedin.browser.registry import resolve_profile, get_or_create_session

        linkedin_profile = resolve_profile(options["username"])
        if linkedin_profile is None:
            logger.error("No LinkedInProfile found for username '%s'.", options["username"])
            sys.exit(1)

        session = get_or_create_session(linkedin_profile)
        interactive_setup(session)
        logger.info("Setup complete for %s. Restart in normal mode to run the daemon.", options["username"])
