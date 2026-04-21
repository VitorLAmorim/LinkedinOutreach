# linkedin/browser/login.py
import logging
import time
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from termcolor import colored

from linkedin.browser.nav import goto_page, human_type
from linkedin.conf import (
    BROWSER_DEFAULT_TIMEOUT_MS,
    BROWSER_LOGIN_CHALLENGE_TIMEOUT_MS,
    BROWSER_LOGIN_TIMEOUT_MS,
    BROWSER_SLOW_MO,
)
from linkedin.exceptions import LoginFailed

# Heartbeat cadence inside long login waits. Must be well below
# STALE_CLAIM_TIMEOUT (180s) so a wait of several minutes still refreshes
# the claim often enough that no other worker steals it.
_LOGIN_HEARTBEAT_INTERVAL_MS = 15_000

logger = logging.getLogger(__name__)

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"

SELECTORS = {
    "email": 'input#username',
    "password": 'input#password',
    "submit": 'button[type="submit"]',
}


def playwright_login(session: "AccountSession"):
    """Full interactive login flow against LinkedIn.

    Any selector timeout, unexpected page layout, or other Playwright error
    raised inside this function is caught and re-raised as ``LoginFailed`` so
    callers (``_fresh_login``, ``_refresh_account_binding``) can release the
    pool claim and retry instead of crashing the worker. Only ``LoginFailed``
    and ``PlaywrightError`` subclasses are translated — programmer errors
    (``AttributeError`` etc.) still propagate so bugs stay visible.
    """
    page = session.page
    account = session.account
    logger.info(colored("Fresh login sequence starting", "cyan") + f" for {session}")

    try:
        goto_page(
            session,
            action=lambda: page.goto(LINKEDIN_LOGIN_URL),
            expected_url_pattern="/login",
            error_message="Failed to load login page",
        )

        human_type(page.locator(SELECTORS["email"]), account.linkedin_username)
        session.wait()
        human_type(page.locator(SELECTORS["password"]), account.linkedin_password)
        session.wait()

        page.locator(SELECTORS["submit"]).click()

        # After submit LinkedIn lands on one of:
        #   1. /feed — happy path, done
        #   2. /checkpoint/challenge/... — CAPTCHA / email code / phone code;
        #      the operator must solve it manually via the worker's VNC window
        # Wait a short timeout for either destination to appear, then branch.
        _wait_for_login_redirect(
            session,
            predicate=lambda url: "/feed" in url or "/checkpoint/" in url,
            total_timeout_ms=BROWSER_LOGIN_TIMEOUT_MS,
        )

        if "/checkpoint/" in page.url:
            logger.warning(
                colored("LinkedIn challenge page detected", "yellow", attrs=["bold"])
                + " for %s — solve the CAPTCHA / verification manually via VNC. "
                "Waiting up to %d minutes for the URL to leave /checkpoint/...",
                session, BROWSER_LOGIN_CHALLENGE_TIMEOUT_MS // 60_000,
            )
            _wait_for_login_redirect(
                session,
                predicate=lambda url: "/feed" in url,
                total_timeout_ms=BROWSER_LOGIN_CHALLENGE_TIMEOUT_MS,
            )

        session.wait()
        current = page.url
        if "/feed" not in current:
            raise LoginFailed(
                f"Login failed – no redirect to feed → got '{current}'"
            )
    except LoginFailed:
        raise  # already the right type
    except (PlaywrightTimeoutError, PlaywrightError) as e:
        # Selector not found, navigation timeout, browser disconnected, etc.
        # LinkedIn occasionally serves a different login layout (A/B variants,
        # cookie-consent interstitials, rate-limit banners) that makes
        # input#username invisible. Treat all of these as recoverable.
        raise LoginFailed(
            f"Login flow failed for {account.username} → "
            f"{type(e).__name__}: {e}"
        ) from e
    except RuntimeError as e:
        # goto_page raises RuntimeError on URL mismatch. Convert so the
        # recovery path in start_browser_session handles it uniformly.
        raise LoginFailed(f"Login navigation failed: {e}") from e


def _wait_for_login_redirect(session, predicate, total_timeout_ms: int) -> None:
    """Wait for ``page.url`` to satisfy ``predicate`` while heartbeating.

    Unlike a plain ``page.wait_for_url(..., timeout=total_timeout_ms)``, this
    helper polls in short bursts (``_LOGIN_HEARTBEAT_INTERVAL_MS`` each) and
    refreshes the pool claim between bursts. This is critical on long waits
    like the CAPTCHA path (up to 10 minutes) — without cooperative
    heartbeating, the claim goes stale after 180s and another worker can
    steal the account while this one is still blocked on the login page.

    Raises ``LoginFailed`` immediately if the claim is stolen mid-wait (so
    the daemon releases and rebinds instead of racing with the new owner).
    Returns normally when the predicate matches or the total timeout expires;
    the caller is responsible for checking ``page.url`` afterwards.
    """
    from linkedin.accounts.pool import heartbeat

    page = session.page
    worker_id = session._worker_id
    elapsed_ms = 0
    while elapsed_ms < total_timeout_ms:
        burst = min(_LOGIN_HEARTBEAT_INTERVAL_MS, total_timeout_ms - elapsed_ms)
        try:
            page.wait_for_url(predicate, timeout=burst)
            return  # predicate matched within this burst — done
        except PlaywrightTimeoutError:
            pass
        elapsed_ms += burst

        # Still waiting — re-heartbeat the pool claim so the daemon loop's
        # 180s stale timeout doesn't kick in during a long captcha wait.
        if worker_id and getattr(session.account, "pk", None):
            owned = heartbeat(session.account, worker_id)
            if not owned:
                raise LoginFailed(
                    f"Claim on {session.account.username} was stolen during "
                    f"login wait (elapsed {elapsed_ms // 1000}s) — aborting"
                )


def _build_proxy_config(url: str | None) -> dict | None:
    # Playwright requires username/password as separate fields — it does not
    # extract credentials embedded in the server URL. Parse them out so that
    # authenticated proxies (e.g. webshare rotating residential) work.
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError(f"PROXY_URL={url!r} has no hostname")
    server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    proxy: dict = {"server": server}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def _resolve_proxy_url(account=None) -> str:
    """Per-account proxy_url wins over the global PROXY_URL env var."""
    from linkedin.conf import PROXY_URL

    if account is not None and getattr(account, "proxy_url", ""):
        return account.proxy_url
    return PROXY_URL or ""


def launch_browser(storage_state=None, account=None):
    logger.debug("Launching Playwright")
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=False, slow_mo=BROWSER_SLOW_MO)
    proxy_url = _resolve_proxy_url(account)
    proxy = _build_proxy_config(proxy_url)
    if proxy:
        logger.info("Routing browser through proxy %s", proxy["server"])
    context = browser.new_context(storage_state=storage_state, locale="en-US", proxy=proxy)
    context.set_default_timeout(BROWSER_DEFAULT_TIMEOUT_MS)
    Stealth().apply_stealth_sync(context)
    page = context.new_page()
    return page, context, browser, playwright, proxy_url


def _save_cookies(session):
    """Persist Playwright storage state (cookies) to the DB."""
    state = session.context.storage_state()
    session.account.cookie_data = state
    session.account.save(update_fields=["cookie_data"])


def _fresh_login(session: "AccountSession") -> None:
    """Run a fresh ``playwright_login`` and persist cookies on success.

    Shared between the "no saved session" and "saved session invalid" paths
    in ``start_browser_session``. On ``LoginFailed`` it closes the browser
    so the next ``ensure_browser()`` call launches a fresh one instead of
    reusing a half-initialized session stuck on /checkpoint/.
    """
    try:
        playwright_login(session)
    except LoginFailed:
        session.close()
        raise
    _save_cookies(session)
    logger.info(colored("Login successful – session saved", "green", attrs=["bold"]))


def start_browser_session(session: "AccountSession"):
    logger.debug("Configuring browser for %s", session)

    session.account.refresh_from_db(fields=["cookie_data", "proxy_url"])
    cookie_data = session.account.cookie_data

    storage_state = cookie_data if cookie_data else None
    if storage_state:
        logger.info("Loading saved session for %s", session)

    (
        session.page,
        session.context,
        session.browser,
        session.playwright,
        session._launched_with_proxy,
    ) = launch_browser(storage_state=storage_state, account=session.account)

    if not storage_state:
        _fresh_login(session)
    else:
        try:
            goto_page(
                session,
                action=lambda: session.page.goto(LINKEDIN_FEED_URL),
                expected_url_pattern="/feed",
                timeout=BROWSER_DEFAULT_TIMEOUT_MS,
                error_message="Saved session invalid",
            )
        except RuntimeError as e:
            # Cookies were accepted by the browser but LinkedIn redirected us
            # back to /uas/login (session revoked server-side, different IP,
            # suspicious activity detection, etc.). Clear the stale cookies
            # from both the DB and the live browser context and fall through
            # to a fresh login.
            logger.warning(
                colored("Saved session invalid", "yellow", attrs=["bold"])
                + " for %s — clearing cookies and re-logging in (%s)",
                session, e,
            )
            session.account.cookie_data = None
            session.account.save(update_fields=["cookie_data"])
            try:
                session.context.clear_cookies()
            except Exception:
                logger.debug("clear_cookies failed", exc_info=True)
            _fresh_login(session)

    session.page.wait_for_load_state("load")
    logger.info(colored("Browser ready", "green", attrs=["bold"]))


_AUTH_COOKIE_NAME = "li_at"
_SETUP_TIMEOUT_SECONDS = 15 * 60  # 15 minutes
_SETUP_POLL_INTERVAL = 5  # seconds


def interactive_setup(session):
    """Launch browser for manual login via VNC. Polls for auth cookie, saves when found."""
    page, context, browser, playwright, proxy_url = launch_browser(account=session.account)
    session.page = page
    session.context = context
    session.browser = browser
    session.playwright = playwright
    session._launched_with_proxy = proxy_url

    page.goto(LINKEDIN_LOGIN_URL)
    logger.info(
        colored("Browser open at LinkedIn login", "cyan", attrs=["bold"])
        + " — log in manually via VNC. Waiting for authentication..."
    )

    elapsed = 0
    while elapsed < _SETUP_TIMEOUT_SECONDS:
        time.sleep(_SETUP_POLL_INTERVAL)
        elapsed += _SETUP_POLL_INTERVAL
        cookies = context.cookies()
        if any(c["name"] == _AUTH_COOKIE_NAME for c in cookies):
            _save_cookies(session)
            logger.info(colored("Authentication detected — session saved", "green", attrs=["bold"]))
            return

    logger.error("Timed out waiting for login (%d minutes)", _SETUP_TIMEOUT_SECONDS // 60)
    raise SystemExit(1)


if __name__ == "__main__":
    from linkedin.browser.registry import cli_parser, cli_session

    parser = cli_parser("Start a LinkedIn browser session")
    args = parser.parse_args()
    session = cli_session(args)
    session.ensure_browser()

    start_browser_session(session=session)
    print("Logged in! Close browser manually.")
    session.page.pause()
