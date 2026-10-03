"""Read-only diagnostic capture for the Humble keys page.

Goal
----
Produce a single shareable .zip archive of artifacts that lets a maintainer
debug selector or API-shape changes WITHOUT exposing the user's actual keys
or PII.

What we capture (per-page HTML + JSON XHR responses + console + screenshots):

  diagnose-<timestamp>/
    raw/                  ← contains the user's real keys; NEVER share
      page-01.html
      page-01.png
      api/
        order-list.json
        order-XXXX.json
      console.log
      trace.zip
    safe-to-share/        ← sanitised copies, OK to attach to a GH issue
      page-01.html
      page-01.png
      api/...
      console.log
      manifest.json
    safe-to-share.zip     ← convenience zip of the above

Sanitisation rules
------------------
* Any key-shaped substring (XXXXX-XXXXX-XXXXX or 12+ alphanum block) →
  ``REDACTED-KEY``
* Any email address →                   ``REDACTED@example.com``
* Humble ``gamekey`` URL params →       ``REDACTED-GAMEKEY``
* Any ``redeemed_key_val`` in JSON →    ``"REDACTED-KEY"``
* Any ``customer_email`` / ``download_email`` JSON field → REDACTED
* Order ``human_name``s and ``machine_name``s are left intact —
  those are public bundle/game names.

The sanitiser is deliberately conservative: better a redaction over-strips
than a key leaks. If you spot a value the sanitiser missed, file a PR.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Page,
    Response,
    sync_playwright,
)

from humble_bundle_keys import __version__
from humble_bundle_keys.auth import AuthOptions, get_authenticated_context

KEYS_URL = "https://www.humblebundle.com/home/keys"

# Same shape as scraper.KEY_PATTERN but anchored as a word boundary on both sides.
KEY_PATTERN = re.compile(
    r"\b([A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8}){1,4}|[A-Z0-9]{12,25})\b"
)
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Humble's gamekey URL parameter is a 16-char alnum string.
GAMEKEY_URL_PARAM = re.compile(r"(gamekey=)[A-Za-z0-9]{6,32}")

# JSON fields whose values must always be redacted.
SENSITIVE_JSON_KEYS = {
    "redeemed_key_val",
    "key_val",
    "customer_email",
    "download_email",
    "purchase_email",
    "buyer_email",
    "preorder_email",
    "purchaser_email",
    "user_id",
    "userid",
    "uid",
    "session_id",
    "shipping_address",
    "shipping_address_line_1",
    "shipping_address_line_2",
    "phone_number",
}

# HTTP headers we never write to disk — these contain auth secrets that would
# let anyone replay the user's session if they got hold of the bundle.
SENSITIVE_HEADERS = {
    "cookie",
    "set-cookie",
    "authorization",
    "proxy-authorization",
    "csrf-prevention-token",
    "x-csrf-token",
    "x-csrftoken",
    "x-xsrf-token",
}

# Static-asset extensions we don't bother capturing — they bloat the bundle
# and have no diagnostic value.
STATIC_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".mjs", ".map",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".webm", ".mp3", ".ogg", ".wav",
    ".pdf",
}

# Cap the size of any single captured response body. Some endpoints return
# huge HTML/JSON blobs that aren't useful for spec discovery and just bloat
# the bundle.
MAX_CAPTURED_BODY_BYTES = 256 * 1024  # 256 KB


def _redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Strip auth-bearing headers from a header dict before saving."""
    if not headers:
        return {}
    return {k: v for k, v in headers.items() if k.lower() not in SENSITIVE_HEADERS}


def _is_static_asset(url: str) -> bool:
    """Heuristic: is this URL just a static asset we don't need to capture?"""
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return any(path.endswith(ext) for ext in STATIC_EXTENSIONS)

log = logging.getLogger(__name__)


@dataclass
class DiagnoseOptions:
    output_dir: Path
    pages: int = 2  # how many pages of the keys table to capture
    headless: bool = True
    # If set, ALSO visit /membership/<slug> in a headed browser and capture
    # XHRs while the user manually clicks "Get Game on Steam" once. Used to
    # discover the Choice claim API shape — see docs/CHOICE_CLAIM_SPEC.md.
    membership_page: str | None = None
    # How long (seconds) to leave the membership browser open for the user
    # to interact with. Default 10 minutes.
    membership_wait_s: int = 600


def _sanitise_text(s: str) -> str:
    """Run all the regex-based sanitisations over an arbitrary string."""
    s = KEY_PATTERN.sub("REDACTED-KEY", s)
    s = EMAIL_PATTERN.sub("REDACTED@example.com", s)
    s = GAMEKEY_URL_PARAM.sub(r"\1REDACTED-GAMEKEY", s)
    return s


def _sanitise_json(obj: Any) -> Any:
    """Recursively sanitise a JSON-decoded structure."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k.lower() in SENSITIVE_JSON_KEYS:
                if isinstance(v, str):
                    out[k] = "REDACTED-KEY" if "key" in k.lower() else "REDACTED"
                elif v is None:
                    out[k] = None
                else:
                    out[k] = "REDACTED"
            else:
                out[k] = _sanitise_json(v)
        return out
    if isinstance(obj, list):
        return [_sanitise_json(x) for x in obj]
    if isinstance(obj, str):
        return _sanitise_text(obj)
    return obj


# ---------------------------------------------------------------------------
# Capture phase
# ---------------------------------------------------------------------------

@dataclass
class _CaptureRegistry:
    """Holds state shared between the page event handler and the caller.

    The caller can flip ``phase`` mid-run (e.g. from "keys" to "membership")
    so each captured file is tagged with the phase it came from.
    """

    api_dir: Path
    phase: str = "keys"
    counter: int = 0
    saved: list[str] = field(default_factory=list)
    saved_by_phase: dict[str, list[str]] = field(default_factory=dict)


def _attach_response_capture(page: Page, api_dir: Path) -> _CaptureRegistry:
    """Listen for any non-asset XHR on humblebundle.com and save it to api_dir.

    Captures BOTH the request (method, URL, headers, body) and the response
    (status, headers, body) for each call. Strips Cookie / Authorization /
    CSRF headers before writing — those are session secrets we never want
    on disk in a shareable bundle.
    """
    api_dir.mkdir(parents=True, exist_ok=True)
    reg = _CaptureRegistry(api_dir=api_dir)

    def on_response(resp: Response) -> None:
        try:
            url = resp.url
            if not url.startswith("https://www.humblebundle.com/"):
                return
            if _is_static_asset(url):
                return

            method = resp.request.method
            ct = (resp.headers.get("content-type") or "").lower()

            # State-changing methods are always interesting. For GETs, only
            # capture if the response looks like JSON — saves us from saving
            # every SPA HTML chunk.
            is_state_change = method.upper() in ("POST", "PUT", "PATCH", "DELETE")
            looks_like_json = "json" in ct or url.endswith(".json")
            if not is_state_change and not looks_like_json:
                return

            # Response body — try JSON first, fall back to (truncated) text.
            response_body: Any
            try:
                response_body = resp.json()
            except Exception:
                try:
                    text = resp.text() or ""
                    if len(text) > MAX_CAPTURED_BODY_BYTES:
                        text = text[:MAX_CAPTURED_BODY_BYTES] + "...[truncated]"
                    response_body = {"_raw_text": text}
                except Exception:
                    response_body = {"_unreadable": True}

            # Request body — Playwright exposes post_data for POST/PUT/etc.
            request_body: Any = None
            try:
                pd = resp.request.post_data
                if pd is not None:
                    # Try to parse as JSON, fall back to raw string.
                    try:
                        request_body = json.loads(pd)
                    except Exception:
                        request_body = pd
            except Exception:
                pass

            reg.counter += 1
            slug = re.sub(r"[^a-zA-Z0-9]+", "-", url.split("?")[0].split("/")[-1])[-40:]
            fname = f"{reg.counter:03d}-{reg.phase}-{slug or 'response'}.json"
            payload = {
                "phase": reg.phase,
                "url": url,
                "status": resp.status,
                "method": method,
                "request_headers": _redact_headers(dict(resp.request.headers)),
                "request_body": request_body,
                "response_headers": _redact_headers(dict(resp.headers)),
                "response_body": response_body,
            }
            (api_dir / fname).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            reg.saved.append(fname)
            reg.saved_by_phase.setdefault(reg.phase, []).append(fname)
        except Exception as e:
            log.debug("Failed capturing response: %s", e)

    page.on("response", on_response)
    return reg


def _capture_console(page: Page, console_log: Path) -> None:
    """Pipe browser console messages to a file."""
    fh = console_log.open("w", encoding="utf-8")

    def on_console(msg):
        try:
            fh.write(f"[{msg.type}] {msg.text}\n")
            fh.flush()
        except Exception:
            pass

    page.on("console", on_console)


def _capture_membership_page(
    page: Page, raw: Path, slug: str, wait_s: int, registry: _CaptureRegistry
) -> None:
    """Reuse ``page`` (the same one used for the keys-page capture) to navigate
    to /membership/<slug>, let the user click 'Get Game on Steam' once, and
    capture all XHRs that result.

    Used to discover the Choice claim flow's API shape — see
    docs/CHOICE_CLAIM_SPEC.md. We intentionally do NOT automate the click: a
    'GET GAME ON STEAM' click consumes a key on Humble's side, so we let the
    user pick which game.

    Reuses the existing page (and its already-attached response listener)
    rather than opening a new one. Avoids the "two browser windows" issue
    where ``context.new_page()`` in headed Chromium opens a new window.

    Three signals can end the capture phase:
      1. User presses ENTER in the terminal (recommended — most reliable).
      2. The browser tab/window is closed (best-effort detection).
      3. ``wait_s`` seconds elapse.
    """
    membership_dir = raw / "membership"
    membership_dir.mkdir(parents=True, exist_ok=True)

    # Switch the capture registry into "membership" phase so every XHR from
    # this point on is tagged distinctly in its filename.
    registry.phase = "membership"
    membership_start_count = registry.counter

    url = f"https://www.humblebundle.com/membership/{slug}"
    log.info("Navigating same browser tab to %s", url)
    try:
        page.goto(url, wait_until="networkidle", timeout=30_000)
    except Exception as e:
        log.warning("Navigation to %s had issues (%s); continuing anyway.", url, e)

    # Save the initial state.
    try:
        (membership_dir / "01-initial.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(membership_dir / "01-initial.png"), full_page=True)
    except Exception as e:
        log.warning("Failed to capture initial membership-page state: %s", e)

    # Set up the Enter-key signal in a background thread.
    done_input = threading.Event()

    def _wait_for_input() -> None:
        try:
            input()
        except Exception:
            pass
        done_input.set()

    threading.Thread(target=_wait_for_input, daemon=True).start()

    # Tell the user what to do.
    print(
        "\n"
        "============================================================\n"
        "  MEMBERSHIP CAPTURE IS NOW LIVE\n"
        "\n"
        "  In the browser window (the same one that's already open):\n"
        "    1. Click ONE game card you haven't claimed yet\n"
        "    2. Click 'GET GAME ON STEAM' in the modal\n"
        "    3. Wait 3-10s for the modal to show the key\n"
        "\n"
        "  When done, signal completion in ANY of these ways:\n"
        "    > Press ENTER in this terminal  (most reliable)\n"
        "    > Close the browser tab/window  (best-effort)\n"
        f"    > Wait up to {wait_s}s for auto-timeout\n"
        "============================================================\n",
        flush=True,
    )

    # Poll for any of the three completion signals.
    deadline = time.time() + wait_s
    stop_reason = "timeout"
    while time.time() < deadline:
        if done_input.is_set():
            stop_reason = "user pressed Enter"
            break
        try:
            if page.is_closed():
                stop_reason = "browser tab closed"
                break
        except Exception:
            stop_reason = "page no longer reachable"
            break
        time.sleep(0.5)
    log.info("Membership capture stopping (%s).", stop_reason)

    # Best-effort final-state capture (skip if the page is gone).
    try:
        if not page.is_closed():
            (membership_dir / "02-final.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(membership_dir / "02-final.png"), full_page=True)
    except Exception as e:
        log.warning("Skipping final-state capture: %s", e)

    # Print a summary so the user immediately knows whether the click was
    # actually captured. If this number is 0, the click went to a URL we're
    # still missing — file a bug.
    captured_during_membership = registry.counter - membership_start_count
    membership_files = registry.saved_by_phase.get("membership", [])
    state_changing = sum(
        1
        for f in membership_files
        # Filenames look like "012-membership-redeemkey.json"; we don't know
        # the method without re-reading, so this is just a hint.
        if "redeem" in f.lower()
        or "claim" in f.lower()
        or "humbler" in f.lower()
    )
    print(
        "\n"
        "============================================================\n"
        f"  MEMBERSHIP-PHASE CAPTURE SUMMARY\n"
        f"  - XHRs captured during this phase: {captured_during_membership}\n"
        f"  - Filenames containing 'redeem'/'claim'/'humbler': {state_changing}\n"
        "\n"
        + (
            "  ✓ Looks like at least one claim-related call was captured.\n"
            "    Check api/ for files tagged '-membership-' to see what fired.\n"
            if state_changing > 0
            else "  ⚠ No obvious claim-call captured. Possible causes:\n"
                 "    - You didn't click 'GET GAME ON STEAM' before pressing Enter\n"
                 "    - The endpoint has a name we don't pattern-match for\n"
                 "    - Look at api/*-membership-*.json anyway — the call may\n"
                 "      be there under a non-obvious filename.\n"
        )
        + "============================================================\n",
        flush=True,
    )


def _capture(opts: DiagnoseOptions) -> Path:
    """Run the live capture. Returns path to the raw/ subdirectory."""
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    base = opts.output_dir / f"diagnose-{timestamp}"
    raw = base / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    api_dir = raw / "api"
    api_dir.mkdir(parents=True, exist_ok=True)

    # If a membership page is requested we need a HEADED browser so the user
    # can interact. The keys-page capture is still done first, as before.
    headless = opts.headless and opts.membership_page is None
    if opts.membership_page is not None and opts.headless:
        log.info(
            "Forcing headed browser because --membership-page requires user interaction."
        )

    auth_opts = AuthOptions(headless=headless)
    with sync_playwright() as p:
        browser, context = get_authenticated_context(p, auth_opts)
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=False)
            page = context.new_page()
            registry = _attach_response_capture(page, api_dir)
            _capture_console(page, raw / "console.log")

            log.info("Loading keys page (read-only)...")
            page.goto(KEYS_URL, wait_until="networkidle")

            for i in range(1, max(1, opts.pages) + 1):
                # Save HTML + screenshot.
                (raw / f"page-{i:02d}.html").write_text(page.content(), encoding="utf-8")
                page.screenshot(path=str(raw / f"page-{i:02d}.png"), full_page=True)
                log.info("Captured page %d", i)
                if i == opts.pages:
                    break
                # Try to advance pagination.
                advanced = False
                for sel in [
                    "a.jump-arrow.right:not(.disabled)",
                    "[class*='pagination'] [aria-label='Next']",
                    "button[aria-label='Next page']",
                ]:
                    loc = page.locator(sel).first
                    if loc.count():
                        try:
                            loc.click(timeout=3000)
                            page.wait_for_load_state("networkidle", timeout=10_000)
                            advanced = True
                            break
                        except Exception:
                            continue
                if not advanced:
                    log.info("No more pages available; stopping early.")
                    break

            # Optional second phase: capture a /membership/<slug> interaction.
            # We reuse the SAME page so only one browser window is ever visible.
            # The response listener attached above continues capturing into api_dir,
            # but we flip the registry's phase to "membership" so per-file
            # filenames are tagged accordingly.
            if opts.membership_page:
                _capture_membership_page(
                    page, raw, opts.membership_page, opts.membership_wait_s, registry
                )

            trace_path = raw / "trace.zip"
            context.tracing.stop(path=str(trace_path))
        finally:
            context.close()
            browser.close()

    # Drop a metadata file at the top of base/.
    (base / "manifest.json").write_text(
        json.dumps(
            {
                "tool": "humble-bundle-keys",
                "version": __version__,
                "captured_at": timestamp,
                "pages_captured": opts.pages,
                "schema": "diagnose-v1",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return base


# ---------------------------------------------------------------------------
# Sanitise phase
# ---------------------------------------------------------------------------

def _sanitise_html_file(src: Path, dst: Path) -> None:
    text = src.read_text(encoding="utf-8", errors="replace")
    dst.write_text(_sanitise_text(text), encoding="utf-8")


def _sanitise_json_file(src: Path, dst: Path) -> None:
    try:
        obj = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        # Fall back to text sanitisation
        dst.write_text(_sanitise_text(src.read_text(encoding="utf-8")), encoding="utf-8")
        return
    cleaned = _sanitise_json(obj)
    dst.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")


def _sanitise_console_file(src: Path, dst: Path) -> None:
    text = src.read_text(encoding="utf-8", errors="replace")
    dst.write_text(_sanitise_text(text), encoding="utf-8")


def sanitise_capture(base: Path) -> Path:
    """Build base/safe-to-share/ from base/raw/. Returns the safe dir."""
    raw = base / "raw"
    safe = base / "safe-to-share"
    if safe.exists():
        shutil.rmtree(safe)
    safe.mkdir(parents=True, exist_ok=True)
    (safe / "api").mkdir(parents=True, exist_ok=True)

    # HTML pages
    for src in sorted(raw.glob("page-*.html")):
        _sanitise_html_file(src, safe / src.name)
    # Screenshots — copy verbatim (they're rendered images and our scraper
    # selectors don't depend on visible text). Optional: a future version
    # could OCR + redact, but for now copy.
    for src in sorted(raw.glob("page-*.png")):
        shutil.copy2(src, safe / src.name)
    # API JSON — all XHRs from both phases (keys-page + membership-page) live
    # together here. Identify by URL inside each JSON file.
    for src in sorted((raw / "api").glob("*.json")):
        _sanitise_json_file(src, safe / "api" / src.name)
    # Membership-page HTML + screenshots, if present.
    mem_raw = raw / "membership"
    if mem_raw.exists():
        mem_safe = safe / "membership"
        mem_safe.mkdir(parents=True, exist_ok=True)
        for src in sorted(mem_raw.glob("*.html")):
            _sanitise_html_file(src, mem_safe / src.name)
        for src in sorted(mem_raw.glob("*.png")):
            shutil.copy2(src, mem_safe / src.name)
        if (mem_raw / "console.log").exists():
            _sanitise_console_file(mem_raw / "console.log", mem_safe / "console.log")
    # Console
    if (raw / "console.log").exists():
        _sanitise_console_file(raw / "console.log", safe / "console.log")
    # Manifest (copy verbatim)
    if (base / "manifest.json").exists():
        shutil.copy2(base / "manifest.json", safe / "manifest.json")

    # NOTE: we deliberately do NOT include trace.zip in safe-to-share/.
    # Trace files contain network HAR + DOM snapshots that are very hard to
    # sanitise reliably. Users can replay it locally with `playwright show-trace`.

    return safe


def bundle_zip(safe_dir: Path) -> Path:
    """Zip the safe-to-share directory."""
    out = safe_dir.parent / "safe-to-share.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in safe_dir.rglob("*"):
            zf.write(p, arcname=p.relative_to(safe_dir.parent))
    return out


# ---------------------------------------------------------------------------
# Entry point used by the CLI
# ---------------------------------------------------------------------------

def run_diagnose(opts: DiagnoseOptions) -> Path:
    base = _capture(opts)
    safe = sanitise_capture(base)
    z = bundle_zip(safe)
    log.info("Capture complete:\n  raw (do NOT share): %s\n  safe to share: %s", base / "raw", z)
    return z
