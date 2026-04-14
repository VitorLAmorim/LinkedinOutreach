# linkedin/browser/session.py
from __future__ import annotations

import logging
import random
import time
from functools import cached_property

from linkedin.conf import MIN_DELAY, MAX_DELAY

logger = logging.getLogger(__name__)

# The main LinkedIn auth cookie
_AUTH_COOKIE_NAME = "li_at"


def random_sleep(min_val, max_val):
    delay = random.uniform(min_val, max_val)
    logger.debug(f"Pause: {delay:.2f}s")
    time.sleep(delay)


class AccountSession:
    def __init__(self, account):
        self.account = account

        # Active campaign — set by the daemon before each lane execution
        self.campaign = None

        # Playwright objects – created on first access or after crash
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

        # Proxy string the current browser was launched with. Set by
        # launch_browser(); compared against the live account.proxy_url by
        # the daemon to detect runtime proxy changes.
        self._launched_with_proxy: str | None = None

        # Worker id (container hostname) in pool mode. Populated by the
        # daemon via bind_worker_id() so the SIGTERM handler can release
        # the claim cleanly.
        self._worker_id: str = ""

    def bind_worker_id(self, worker_id: str) -> None:
        """Tell the session which worker owns it (for claim release on shutdown)."""
        self._worker_id = worker_id

    def swap_account(self, new_account) -> None:
        """Swap the bound LinkedInAccount. Browser must already be closed."""
        if self.page is not None or self.context is not None:
            raise RuntimeError("swap_account called with a live browser — close it first")
        logger.info("Swapping session: %s → %s", self.account.username, new_account.username)
        self.account = new_account
        self.campaign = None
        self.invalidate_campaigns_cache()
        self.__dict__.pop("self_profile", None)
        self._launched_with_proxy = None

    @cached_property
    def campaigns(self):
        """All campaigns for this account (cached)."""
        from linkedin.models import Campaign
        return list(Campaign.objects.filter(account=self.account, active=True))

    def invalidate_campaigns_cache(self):
        """Drop the cached campaigns list so the next access re-queries the DB."""
        self.__dict__.pop("campaigns", None)

    def ensure_browser(self):
        """Launch or recover browser + login if needed. Call before using .page"""
        from linkedin.browser.login import start_browser_session

        if not self.page or self.page.is_closed():
            logger.debug("Launching/recovering browser for %s", self)
            start_browser_session(session=self)
        else:
            self._maybe_refresh_cookies()

    @cached_property
    def self_profile(self) -> dict:
        """Lazy accessor: return the authenticated user's profile dict (cached).

        Reads from ``self_lead.profile_data`` if available, otherwise
        discovers via Voyager API and persists.
        """
        self.account.refresh_from_db(fields=["self_lead"])
        lead = self.account.self_lead
        if lead and lead.profile_data and "urn" in lead.profile_data:
            return lead.profile_data

        from linkedin.setup.self_profile import discover_self_profile

        self.ensure_browser()
        return discover_self_profile(self)

    def wait(self, min_delay=MIN_DELAY, max_delay=MAX_DELAY):
        random_sleep(min_delay, max_delay)
        self.page.wait_for_load_state("load")

    def reauthenticate(self):
        """Force a fresh login: close browser, clear saved cookies, re-launch."""
        from linkedin.browser.login import start_browser_session

        logger.warning("Re-authenticating %s — clearing saved session", self)
        self.close()
        self.account.cookie_data = None
        self.account.save(update_fields=["cookie_data"])
        start_browser_session(session=self)

    def _maybe_refresh_cookies(self):
        """Re-login if the li_at auth cookie in the saved DB state is expired."""
        from linkedin.browser.login import start_browser_session

        self.account.refresh_from_db(fields=["cookie_data"])
        cookie_data = self.account.cookie_data
        if not cookie_data:
            return
        for cookie in cookie_data.get("cookies", []):
            if cookie.get("name") == _AUTH_COOKIE_NAME:
                expires = cookie.get("expires", -1)
                if expires > 0 and expires < time.time():
                    logger.warning("Auth cookie expired for %s — re-authenticating", self)
                    self.close()
                    start_browser_session(session=self)
                return

    def close(self):
        if self.context:
            try:
                self.context.close()
                if self.browser:
                    self.browser.close()
                if self.playwright:
                    self.playwright.stop()
                logger.info("Browser closed gracefully (%s)", self)
            except Exception as e:
                logger.debug("Error closing browser: %s", e)
            finally:
                self.page = self.context = self.browser = self.playwright = None
                self._launched_with_proxy = None

        logger.info("Account session closed → %s", self)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return getattr(self.account, "username", "<unbound>")


def install_shutdown_handler(session: "AccountSession") -> None:
    """Register SIGTERM/SIGINT handlers that release the session's pool claim.

    Without this, `docker stop` leaves the claim row owned by a dead worker
    until STALE_CLAIM_TIMEOUT expires. With it, the claim frees within ms.
    """
    import signal

    def _handler(signum, frame):
        logger.warning("Received signal %s — releasing claim and closing browser", signum)
        try:
            if session._worker_id:
                from linkedin.accounts.pool import release_account
                release_account(session.account, session._worker_id)
        except Exception:
            logger.exception("Failed to release claim on shutdown")
        try:
            session.close()
        except Exception:
            logger.exception("Failed to close session on shutdown")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
