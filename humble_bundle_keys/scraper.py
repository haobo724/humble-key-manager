"""Scraper for the Humble Bundle Keys & Entitlements page.

Design notes
------------
The Keys & Entitlements page (/home/keys) is rendered client-side by Humble's
frontend SPA. Two row shapes appear in its paginated table:

* **Bundle rows** — e.g. "MARCH 2026 HUMBLE CHOICE" with a "GET MY GAMES"
  button. Clicking it opens a modal where the user picks (or auto-claims) the
  games for that bundle, which then appear as individual key rows.

* **Individual key rows** — one game with its platform (Steam, GOG, Origin,
  Uplay, Epic, …), the source bundle, and either a green pill containing the
  already-revealed key, or a "Redeem" button that reveals **and** marks the
  key as claimed in Humble's system.

This scraper:

1. Walks every page of the table.
2. For each individual key row, extracts metadata. If the key isn't already
   visible and ``reveal_keys`` is True, clicks "Redeem" to reveal it.
3. Optionally expands bundle rows and triggers "REDEEM ALL ON YOUR ACCOUNT"
   when the bundle is one of the auto-claimable kinds.

Selectors are deliberately written with multiple fallbacks because Humble
ships frontend redesigns periodically. If a selector starts missing, raise
an issue with a debug dump (``--debug``) attached.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from humble_bundle_keys.models import ExtractStats, GameKey

KEYS_URL = "https://www.humblebundle.com/home/keys"

# Heuristics: a key looks like 5-25 alphanumeric chars with optional dashes,
# at least 8 chars total, and must contain at least one digit OR have the
# tell-tale XXXXX-XXXXX-XXXXX shape Steam/Origin/etc. use.
KEY_PATTERN = re.compile(r"\b([A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8}){1,4}|[A-Z0-9]{12,25})\b")
# Common platform names — match case-insensitively.
PLATFORMS = {
    "steam": "steam",
    "gog": "gog",
    "origin": "origin",
    "ea origin": "origin",
    "ea app": "origin",
    "uplay": "uplay",
    "ubisoft connect": "uplay",
    "epic games store": "epic",
    "epic": "epic",
    "battle.net": "battlenet",
    "battlenet": "battlenet",
    "rockstar": "rockstar",
    "xbox": "xbox",
    "ms store": "microsoft",
    "microsoft": "microsoft",
    "drm-free": "drmfree",
}

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Selector candidates. Each list is tried in order; first hit wins.
# Keep these maintainable — when Humble changes its DOM, you'll edit them here.
# ---------------------------------------------------------------------------

SEL = {
    # Container holding the table of keys.
    "table_container": [
        "[class*='keys-table']",
        "[class*='entitlements']",
        "table.keys-table",
        "div.keys-and-entitlements",
    ],
    # Each row in the table (both bundle rows and key rows).
    "row": [
        "tr.keys-table__row",
        "[class*='keys-table__row']",
        "tbody tr",
        "[class*='entitlement-row']",
    ],
    # The "GET MY GAMES" button on bundle rows.
    "get_my_games_button": [
        "text=/get my games/i",
        "button:has-text('GET MY GAMES')",
        "[class*='get-games']",
    ],
    # The Redeem button next to an individual key.
    "redeem_button": [
        "button.js-redeem-button",
        "text=/^redeem$/i",
        "[class*='redeem-button']",
        "[class*='keyfield-redeem']",
    ],
    # The element holding the revealed key text.
    "key_text": [
        "[class*='keyfield-value']",
        "[class*='keyfield']",
        "input[readonly]",
        "[class*='key-value']",
    ],
    # Pagination "next" arrow.
    "pagination_next": [
        "a.jump-arrow.right:not(.disabled)",
        "[class*='pagination'] [aria-label='Next']",
        "button[aria-label='Next page']",
        "li.pagination-next a",
    ],
    "pagination_active_page": [
        "[class*='pagination'] [class*='active']",
        "li.active",
    ],
    # Generic modal close (X).
    "modal_close": [
        "button[aria-label='Close']",
        "[class*='close-button']",
        "[class*='modal'] button.close",
        ".js-close-modal",
    ],
    # Modal "Redeem all on your account" button.
    "redeem_all": [
        "text=/redeem all on your account/i",
        "button:has-text('REDEEM ALL ON YOUR ACCOUNT')",
    ],
    # Hide-redeemed checkbox; we want it UNchecked so we see everything.
    "hide_redeemed_checkbox": [
        "input[type='checkbox'][name*='hide']",
        "label:has-text('Hide redeemed')",
    ],
    # Cookie / GDPR banners that sometimes overlay the page.
    "cookie_dismiss": [
        "button:has-text('Accept')",
        "button:has-text('OK')",
        "button:has-text('Got it')",
        "[aria-label='Close cookie banner']",
    ],
}


def _first_visible(page_or_locator: Page | Locator, candidates: list[str]) -> Locator | None:
    """Return the first visible Locator from a list of selector candidates."""
    for sel in candidates:
        try:
            loc = page_or_locator.locator(sel).first
            if loc.count() and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _all_matches(page_or_locator: Page | Locator, candidates: list[str]) -> Locator | None:
    """Return *all* matches for the first selector that has any matches."""
    for sel in candidates:
        try:
            loc = page_or_locator.locator(sel)
            if loc.count():
                return loc
        except Exception:
            continue
    return None


@dataclass
class ScrapeOptions:
    reveal_keys: bool = True  # click Redeem on hidden keys to unmask + claim
    expand_bundles: bool = False  # click GET MY GAMES on Humble Choice rows
    dry_run: bool = False  # don't click anything that mutates server state
    polite_delay_ms: int = 800  # delay between key reveals to be a good citizen
    max_pages: int | None = None  # cap pages walked (debugging)
    debug_dir: Path | None = None  # if set, save screenshots on errors
    page_load_timeout_ms: int = 30_000


@dataclass
class _RowExtraction:
    is_bundle: bool
    game: GameKey | None = None
    bundle_name: str = ""
    notes: list[str] = field(default_factory=list)


class KeysScraper:
    def __init__(self, context: BrowserContext, options: ScrapeOptions):
        self.context = context
        self.options = options
        self.page: Page = context.new_page()
        self.stats = ExtractStats()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scrape(self) -> tuple[list[GameKey], ExtractStats]:
        results: list[GameKey] = []
        self.page.set_default_timeout(self.options.page_load_timeout_ms)

        log.info("Loading %s", KEYS_URL)
        self.page.goto(KEYS_URL, wait_until="domcontentloaded")
        self._dismiss_cookie_banners()
        self._uncheck_hide_redeemed()
        self._wait_for_table()

        page_idx = 0
        while True:
            page_idx += 1
            if self.options.max_pages and page_idx > self.options.max_pages:
                log.info("Reached --max-pages=%d, stopping.", self.options.max_pages)
                break

            log.info("Scraping page %d", page_idx)
            page_results = self._scrape_current_page()
            results.extend(page_results)

            if not self._goto_next_page():
                log.info("No more pages.")
                break
            # tiny pause for stability
            self.page.wait_for_load_state("domcontentloaded")
            time.sleep(0.5)

        self.stats.total_rows = len(results)
        return results, self.stats

    # ------------------------------------------------------------------
    # Page setup helpers
    # ------------------------------------------------------------------

    def _dismiss_cookie_banners(self) -> None:
        loc = _first_visible(self.page, SEL["cookie_dismiss"])
        if loc:
            with self._swallow("dismiss cookie banner"):
                loc.click(timeout=2000)
                self.page.wait_for_timeout(200)

    def _uncheck_hide_redeemed(self) -> None:
        """Make sure 'Hide redeemed keys & entitlements' is unchecked."""
        for sel in SEL["hide_redeemed_checkbox"]:
            try:
                cb = self.page.locator(sel).first
                if cb.count() == 0:
                    continue
                if cb.is_checked():
                    cb.uncheck()
                return
            except Exception:
                continue

    def _wait_for_table(self) -> None:
        for sel in SEL["table_container"]:
            try:
                self.page.wait_for_selector(sel, timeout=5_000, state="visible")
                return
            except PlaywrightTimeoutError:
                continue
        log.warning("Couldn't find a keys table container; will try anyway.")

    # ------------------------------------------------------------------
    # Page walker
    # ------------------------------------------------------------------

    def _scrape_current_page(self) -> list[GameKey]:
        rows_loc = _all_matches(self.page, SEL["row"])
        if rows_loc is None:
            log.warning("No rows found on this page.")
            return []

        n = rows_loc.count()
        log.info("Found %d rows on this page.", n)
        out: list[GameKey] = []
        for i in range(n):
            row = rows_loc.nth(i)
            try:
                extraction = self._handle_row(row)
            except Exception as e:
                err = f"row {i}: {e}"
                self.stats.errors.append(err)
                log.warning(err)
                self._dump_debug(f"row-{i}-error")
                continue

            if extraction.is_bundle:
                self.stats.bundles_processed += 1
            elif extraction.game is not None:
                out.append(extraction.game)
        return out

    def _handle_row(self, row: Locator) -> _RowExtraction:
        # Decide row shape: bundle row has a "GET MY GAMES" button.
        get_games_btn = self._find_in_row(row, SEL["get_my_games_button"])
        if get_games_btn is not None:
            bundle_name = self._row_text_first_line(row)
            if self.options.expand_bundles and not self.options.dry_run:
                self._claim_bundle(get_games_btn, bundle_name)
            return _RowExtraction(is_bundle=True, bundle_name=bundle_name)

        # Otherwise treat as an individual key row.
        return _RowExtraction(is_bundle=False, game=self._extract_key_row(row))

    def _extract_key_row(self, row: Locator) -> GameKey | None:
        text = self._safe_text(row)
        if not text.strip():
            return None

        # Game title is usually the largest piece of text in the row,
        # often duplicated in a [class*='game-name'] / [class*='game-title'] node.
        title = self._first_text_or_empty(
            row,
            [
                "[class*='game-name']",
                "[class*='game-title']",
                "[class*='product-name']",
                "td:nth-child(2)",
            ],
        )
        if not title:
            # Fall back to first non-empty line.
            for line in text.splitlines():
                line = line.strip()
                if line and len(line) > 1 and not line.lower().startswith(("redeem", "key")):
                    title = line
                    break

        platform = self._detect_platform(row, text)
        bundle_name = self._first_text_or_empty(
            row,
            [
                "a[href*='/downloads']",
                "[class*='product-source']",
                "[class*='from-bundle']",
            ],
        )
        deadline = self._extract_deadline(text)
        os_support = self._detect_os(row, text)
        humble_url = self._first_attr_or_empty(row, "a", "href")
        if humble_url and humble_url.startswith("/"):
            humble_url = "https://www.humblebundle.com" + humble_url

        # Try to find an already-visible key first.
        existing_key = self._find_visible_key(row, text)
        revealed_now = False

        if not existing_key and self.options.reveal_keys and not self.options.dry_run:
            redeem_btn = self._find_in_row(row, SEL["redeem_button"])
            if redeem_btn is not None:
                with self._swallow(f"reveal key for {title!r}"):
                    redeem_btn.click()
                    # Wait briefly for the key to materialise.
                    self.page.wait_for_timeout(self.options.polite_delay_ms)
                    existing_key = self._find_visible_key(row, self._safe_text(row))
                    revealed_now = bool(existing_key)
                    if revealed_now:
                        self.stats.keys_revealed += 1

        if existing_key:
            self.stats.keys_already_revealed += 1 if not revealed_now else 0

        return GameKey(
            game_title=title or "(unknown)",
            platform=platform,
            key=existing_key or "",
            bundle_name=bundle_name,
            bundle_date="",  # filled in by post-processing if a date is in bundle_name
            redemption_deadline=deadline,
            redeemed_on_humble=bool(existing_key),
            os_support=os_support,
            humble_url=humble_url,
        )

    def _claim_bundle(self, button: Locator, bundle_name: str) -> None:
        """Open the bundle modal and click 'Redeem all on your account' if present."""
        log.info("Expanding bundle: %s", bundle_name)
        with self._swallow(f"open bundle {bundle_name!r}"):
            button.click()
            self.page.wait_for_timeout(800)
            redeem_all = _first_visible(self.page, SEL["redeem_all"])
            if redeem_all is not None:
                redeem_all.click()
                self.page.wait_for_timeout(1500)
            self._close_modal()

    def _close_modal(self) -> None:
        loc = _first_visible(self.page, SEL["modal_close"])
        if loc is not None:
            with self._swallow("close modal"):
                loc.click()
                self.page.wait_for_timeout(300)
        # As a fallback, press Escape.
        with self._swallow("escape modal"):
            self.page.keyboard.press("Escape")

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _goto_next_page(self) -> bool:
        next_loc = _first_visible(self.page, SEL["pagination_next"])
        if next_loc is None:
            return False
        # Some Humble layouts disable the next button by adding 'disabled' class
        # rather than removing it. Guard against that.
        try:
            klass = (next_loc.get_attribute("class") or "").lower()
            if "disabled" in klass:
                return False
            aria_disabled = (next_loc.get_attribute("aria-disabled") or "").lower()
            if aria_disabled == "true":
                return False
        except Exception:
            pass
        with self._swallow("click next page"):
            next_loc.click()
            return True
        return False

    # ------------------------------------------------------------------
    # Cell-level helpers
    # ------------------------------------------------------------------

    def _find_in_row(self, row: Locator, candidates: list[str]) -> Locator | None:
        for sel in candidates:
            try:
                loc = row.locator(sel).first
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                continue
        return None

    def _safe_text(self, row: Locator) -> str:
        try:
            return row.inner_text(timeout=2_000)
        except Exception:
            return ""

    def _row_text_first_line(self, row: Locator) -> str:
        text = self._safe_text(row)
        for line in text.splitlines():
            line = line.strip()
            if line:
                return line
        return ""

    def _first_text_or_empty(self, row: Locator, candidates: list[str]) -> str:
        for sel in candidates:
            try:
                loc = row.locator(sel).first
                if loc.count():
                    txt = loc.inner_text(timeout=1_500).strip()
                    if txt:
                        return txt
            except Exception:
                continue
        return ""

    def _first_attr_or_empty(self, row: Locator, selector: str, attr: str) -> str:
        try:
            loc = row.locator(selector).first
            if loc.count():
                return loc.get_attribute(attr) or ""
        except Exception:
            pass
        return ""

    def _detect_platform(self, row: Locator, row_text: str) -> str:
        # Try icon class names first (Humble uses class names like 'hb-steam').
        try:
            icons = row.locator("[class*='hb-']").all()
            for ic in icons:
                klass = (ic.get_attribute("class") or "").lower()
                for needle, label in PLATFORMS.items():
                    if needle.replace(" ", "") in klass:
                        return label
        except Exception:
            pass

        # Fall back to alt text on images.
        try:
            imgs = row.locator("img[alt]").all()
            for img in imgs:
                alt = (img.get_attribute("alt") or "").lower()
                for needle, label in PLATFORMS.items():
                    if needle in alt:
                        return label
        except Exception:
            pass

        # Last resort: literal substring in row text.
        lower = row_text.lower()
        for needle, label in PLATFORMS.items():
            if needle in lower:
                return label
        return ""

    def _detect_os(self, row: Locator, row_text: str) -> str:
        oses = []
        for word, label in [
            ("windows", "Windows"),
            ("mac", "macOS"),
            ("linux", "Linux"),
        ]:
            if word in row_text.lower():
                oses.append(label)
        return ", ".join(oses)

    def _extract_deadline(self, row_text: str) -> str:
        """Extract the 'Must be redeemed by ...' line if present."""
        for line in row_text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("must be redeemed"):
                return stripped
        return ""

    def _find_visible_key(self, row: Locator, row_text: str) -> str:
        # Try dedicated key elements first.
        for sel in SEL["key_text"]:
            try:
                loc = row.locator(sel).first
                if loc.count():
                    # Could be an input or a span.
                    val = loc.input_value(timeout=500) if "input" in sel else None
                    if val:
                        m = KEY_PATTERN.search(val.upper())
                        if m:
                            return m.group(1)
                    txt = loc.inner_text(timeout=500).strip().upper()
                    m = KEY_PATTERN.search(txt)
                    if m:
                        return m.group(1)
            except Exception:
                continue

        # Fall back to scanning the whole row text.
        m = KEY_PATTERN.search(row_text.upper())
        if m:
            candidate = m.group(1)
            # Reject obvious false positives like dates / years.
            if candidate.isdigit() and len(candidate) <= 4:
                return ""
            return candidate
        return ""

    # ------------------------------------------------------------------
    # Debugging
    # ------------------------------------------------------------------

    @contextmanager
    def _swallow(self, what: str):
        try:
            yield
        except Exception as e:
            log.debug("Swallowed error during %s: %s", what, e)

    def _dump_debug(self, label: str) -> None:
        if self.options.debug_dir is None:
            return
        try:
            self.options.debug_dir.mkdir(parents=True, exist_ok=True)
            png = self.options.debug_dir / f"{label}.png"
            self.page.screenshot(path=str(png), full_page=True)
            html = self.options.debug_dir / f"{label}.html"
            html.write_text(self.page.content(), encoding="utf-8")
            log.info("Wrote debug artifacts to %s", self.options.debug_dir)
        except Exception as e:
            log.warning("Failed to dump debug: %s", e)
