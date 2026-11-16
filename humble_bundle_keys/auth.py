"""Authentication and session management for humblebundle.com.

Strategy
--------
We never ask the user for their password. There are two supported ways to
get an authenticated browser context:

1. Persisted storage state (default path: ``~/.humble-bundle-keys/storage_state.json``).
   On first run, we open a real browser at auth.humblebundle.com.
   The user completes email or Google sign-in,
   then we verify the server session and save cookies + localStorage to disk.
   Subsequent runs reuse that file headlessly until it expires.

2. ``HUMBLE_SESSION_COOKIE`` environment variable. If set, we build a fresh
   context with just that cookie (the value of Humble's ``_simpleauth_sess``
   cookie). Useful for CI / power users who already have a session cookie
   they want to inject. Takes precedence over storage state.

In both cases, before we hand the context off to the scraper, we navigate
to the official session endpoint and verify the server reports an authenticated session.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from humble_bundle_keys.site_session import LIBRARY_URL, LOGIN_URL, session_status

KEYS_URL = LIBRARY_URL
SESSION_COOKIE_NAME = "_simpleauth_sess"
DEFAULT_STATE_DIR = Path.home() / ".humble-bundle-keys"
DEFAULT_STATE_PATH = DEFAULT_STATE_DIR / "storage_state.json"

# Verify server-side session state rather than a transient client-side URL.
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
    """Check the server session; a visible library shell alone does not prove login."""
    try:
        status = session_status(page.context)
        if status is not None:
            return status == "authenticated"
        response = page.goto(KEYS_URL, wait_until="domcontentloaded",
                             timeout=AUTH_CHECK_TIMEOUT_MS)
        url = urlparse(page.url)
        return (response is not None and response.status == 200
                and url.hostname == "www.humblebundle.com"
                and url.path in {"/home/keys", "/home/library"}
                and bool(re.search(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", page.title())))
    except Exception:
        return False


def _interactive_login(
    playwright: Playwright, state_path: Path, browser_channel: str | None = None,
) -> None:
    """Start a fresh flow; preserve the previous session until the new one is valid."""
    log.info("Opening Humble's official sign-in window; complete email or Google sign-in there.")
    browser = playwright.chromium.launch(headless=False, channel=browser_channel)
    temporary = state_path.with_suffix(".tmp")
    try:
        context = browser.new_context()
        page = context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_url(
                lambda url: urlparse(str(url)).hostname == "www.humblebundle.com"
                and not urlparse(str(url)).path.startswith("/auth/"),
                timeout=LOGIN_WAIT_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError as exc:
            raise AuthError("等待 Humble 登录超时，请完成官网的邮箱或 Google 登录。") from exc
        if not _is_authenticated(page):
            raise AuthError("官网返回后会话仍未验证，请重新完成登录。")
        _ensure_state_dir(state_path)
        context.storage_state(path=str(temporary))
        temporary.replace(state_path)
        log.info("Humble session saved.")
    finally:
        if temporary.exists():
            temporary.unlink()
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
    if _state_exists(state_path) and not options.force_login:
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
