"""Authentication and session management for humblebundle.com.

Strategy
--------
We never ask the user for their password. There are two supported ways to
get an authenticated browser context:

1. Persisted storage state (default path: ``~/.humble-bundle-keys/storage_state.json``).
   On first run, we open a real Chrome window pointed at humblebundle.com's
   login page. The user logs in normally — including Humble's email 2FA — and
   when the keys page is reachable, we save the cookies + localStorage to disk.
   Subsequent runs reuse that file headlessly until it expires.

2. ``HUMBLE_SESSION_COOKIE`` environment variable. If set, we build a fresh
   context with just that cookie (the value of Humble's ``_simpleauth_sess``
   cookie). Useful for CI / power users who already have a session cookie
   they want to inject. Takes precedence over storage state.

In both cases, before we hand the context off to the scraper, we navigate
to /home/keys and confirm we're not redirected to the login page.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

LOGIN_URL = "https://www.humblebundle.com/login"
KEYS_URL = "https://www.humblebundle.com/home/keys"
SESSION_COOKIE_NAME = "_simpleauth_sess"
DEFAULT_STATE_DIR = Path.home() / ".humble-bundle-keys"
DEFAULT_STATE_PATH = DEFAULT_STATE_DIR / "storage_state.json"

# Heuristic: if we land on /home/keys and the URL still contains "keys",
# we're authenticated. If we got redirected to /login, we're not.
AUTH_CHECK_TIMEOUT_MS = 15_000
LOGIN_WAIT_TIMEOUT_MS = 5 * 60 * 1000  # 5 minutes for the user to finish logging in

log = logging.getLogger(__name__)


@dataclass
class AuthOptions:
    storage_state_path: Path = DEFAULT_STATE_PATH
    headless: bool = True
    # If True, never open a headed browser even if state is missing/invalid.
    # Useful for scheduled runs that should fail loudly instead of waiting.
    no_interactive: bool = False
    # If True, force a fresh login even if state exists.
    force_login: bool = False
    browser_channel: str | None = None


class AuthError(RuntimeError):
    """Raised when we can't establish an authenticated session."""


def _ensure_state_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _state_exists(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _build_context_from_cookie(browser: Browser, cookie_value: str) -> BrowserContext:
    """Create a fresh context that has just the Humble session cookie set."""
    context = browser.new_context()
    context.add_cookies(
        [
            {
                "name": SESSION_COOKIE_NAME,
                "value": cookie_value,
                "domain": ".humblebundle.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
            }
        ]
    )
    return context


def _is_authenticated(page: Page) -> bool:
    """Navigate to the keys page and decide whether we're logged in.

    Humble redirects unauthenticated users to /login?goto=... — we treat any
    final URL that doesn't contain ``/home/keys`` as 'not authenticated'.
    """
    try:
        page.goto(KEYS_URL, wait_until="domcontentloaded", timeout=AUTH_CHECK_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        return False
    final_url = page.url or ""
    return "/home/keys" in final_url and "/login" not in final_url


def _interactive_login(
    playwright: Playwright, state_path: Path, browser_channel: str | None = None,
) -> None:
    """Open a real browser, let the user log in, persist state, close."""
    log.info("Opening a browser window for first-time login.")
    log.info("Log in normally — including any 2FA emails — then this will close automatically.")

    browser = playwright.chromium.launch(headless=False, channel=browser_channel)
    context = browser.new_context()
    page = context.new_page()
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    # Wait for the user to land on a page that proves they're authenticated.
    # We poll the URL since Humble's login flow goes through several redirects
    # (and possibly a 2FA email step). We give the user up to 5 minutes.
    try:
        page.wait_for_url(
            lambda url: "/home/" in url or "/account" in url,
            timeout=LOGIN_WAIT_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as e:
        browser.close()
        raise AuthError(
            "Timed out waiting for login. Re-run and finish the Humble login "
            "(including any 2FA email step) within 5 minutes."
        ) from e

    # Final sanity check: actually navigate to /home/keys
    if not _is_authenticated(page):
        browser.close()
        raise AuthError(
            "Login appeared to succeed but /home/keys is still redirecting to login. "
            "Double-check your account and try again."
        )

    _ensure_state_dir(state_path)
    context.storage_state(path=str(state_path))
    log.info("Saved session to %s", state_path)
    browser.close()


def get_authenticated_context(
    playwright: Playwright,
    options: AuthOptions,
) -> tuple[Browser, BrowserContext]:
    """Return a Playwright (browser, context) pair that's logged into Humble.

    The caller owns the lifetime of both — close the browser when done.
    """
    state_path = options.storage_state_path

    # 1. Env-var cookie path takes precedence.
    cookie = os.environ.get("HUMBLE_SESSION_COOKIE", "").strip()
    if cookie:
        log.info("Using HUMBLE_SESSION_COOKIE from environment.")
        browser = playwright.chromium.launch(
            headless=options.headless, channel=options.browser_channel,
        )
        context = _build_context_from_cookie(browser, cookie)
        page = context.new_page()
        if _is_authenticated(page):
            page.close()
            return browser, context
        page.close()
        context.close()
        browser.close()
        raise AuthError(
            "HUMBLE_SESSION_COOKIE is set but the resulting session isn't authenticated. "
            "Refresh the cookie from your logged-in browser and try again."
        )

    # 2. Saved storage state path.
    if options.force_login and _state_exists(state_path):
        log.info("--force-login set; deleting %s", state_path)
        state_path.unlink()

    if _state_exists(state_path):
        browser = playwright.chromium.launch(
            headless=options.headless, channel=options.browser_channel,
        )
        try:
            context = browser.new_context(storage_state=str(state_path))
        except Exception as e:
            log.warning("storage_state at %s appears corrupt (%s); will re-login.", state_path, e)
            browser.close()
        else:
            page = context.new_page()
            if _is_authenticated(page):
                page.close()
                return browser, context
            log.info("Saved session is no longer valid; re-running login.")
            page.close()
            context.close()
            browser.close()

    # 3. Need to do an interactive login.
    if options.no_interactive:
        raise AuthError(
            f"No valid session at {state_path} and --no-interactive was set. "
            "Run once without --no-interactive (or with --force-login) to authenticate."
        )

    _interactive_login(playwright, state_path, options.browser_channel)

    # Re-open with the freshly saved state, this time honouring headless.
    browser = playwright.chromium.launch(
        headless=options.headless, channel=options.browser_channel,
    )
    context = browser.new_context(storage_state=str(state_path))
    page = context.new_page()
    if not _is_authenticated(page):
        page.close()
        context.close()
        browser.close()
        raise AuthError("Login succeeded but reusing the saved state failed. This is a bug.")
    page.close()
    return browser, context


def export_state_for_inspection(state_path: Path = DEFAULT_STATE_PATH) -> dict | None:
    """Read and return the saved storage state, or None if missing."""
    if not _state_exists(state_path):
        return None
    with state_path.open("r", encoding="utf-8") as f:
        return json.load(f)
