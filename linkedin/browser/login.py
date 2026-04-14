# linkedin/browser/login.py
import logging
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from termcolor import colored

from linkedin.browser.nav import goto_page, human_type
from linkedin.conf import (
    BROWSER_DEFAULT_TIMEOUT_MS,
    BROWSER_LOGIN_TIMEOUT_MS,
    BROWSER_SLOW_MO,
)

logger = logging.getLogger(__name__)

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"

SELECTORS = {
    "email": 'input#username',
    "password": 'input#password',
    "submit": 'button[type="submit"]',
}


def playwright_login(session: "AccountSession"):
    page = session.page
    account = session.account
    logger.info(colored("Fresh login sequence starting", "cyan") + f" for {session}")

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

    goto_page(
        session,
        action=lambda: page.locator(SELECTORS["submit"]).click(),
        expected_url_pattern="/feed",
        timeout=BROWSER_LOGIN_TIMEOUT_MS,
        error_message="Login failed – no redirect to feed",
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
        playwright_login(session)
        _save_cookies(session)
        logger.info(colored("Login successful – session saved", "green", attrs=["bold"]))
    else:
        goto_page(
            session,
            action=lambda: session.page.goto(LINKEDIN_FEED_URL),
            expected_url_pattern="/feed",
            timeout=BROWSER_DEFAULT_TIMEOUT_MS,
            error_message="Saved session invalid",
        )

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
