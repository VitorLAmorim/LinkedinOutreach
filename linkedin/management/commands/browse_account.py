import logging
import signal
import sys

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Launch browser with saved cookies for manual VNC navigation (no daemon)."

    def add_arguments(self, parser):
        parser.add_argument("username", help="Django username of the LinkedInProfile")

    def handle(self, *args, **options):
        from linkedin.browser.login import interactive_setup, start_browser_session
        from linkedin.browser.registry import resolve_profile, get_or_create_session

        linkedin_profile = resolve_profile(options["username"])
        if linkedin_profile is None:
            logger.error("No LinkedInProfile found for username '%s'.", options["username"])
            sys.exit(1)

        session = get_or_create_session(linkedin_profile)

        if linkedin_profile.cookie_data:
            try:
                start_browser_session(session)
            except Exception:
                logger.warning("Saved session invalid — falling back to manual login")
                session.close()
                session = get_or_create_session(linkedin_profile)
                interactive_setup(session)
        else:
            interactive_setup(session)

        logger.info("Browse mode active for %s. Use VNC to interact with the browser.", options["username"])
        signal.pause()
