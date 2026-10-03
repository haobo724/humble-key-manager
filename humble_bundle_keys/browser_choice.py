"""Browser-driven Humble Choice claim flow.

This is the path that mirrors what a human does:

1. Visit ``humblebundle.com/membership/<month-slug>``
2. For each unclaimed game card on the page:
   a. Click the card
   b. Wait for the modal to open
   c. Click "GET GAME ON STEAM"
   d. Wait 3-10 s for the modal to update with the key
   e. Read the revealed key out of the modal
   f. Close the modal
3. Move on to the next card / membership page

It exists alongside :mod:`humble_bundle_keys.choice` (the API path) because:

* The API path can only see games already in ``tpkd_dict.all_tpks``. Legacy
  "pick N of M" Choice months only put a tpk into the order *after* the user
  has chosen the game on the membership page; before that, the menu of
  available games lives only in the membership page's rendered DOM.
* For modern "claim everything" Choice months the API path also works, but
  some entries get stuck in a state where ``redeemkey`` returns success
  with no key (Humble bookkeeping issue). The DOM path bypasses that by
  going through the same UI a human would.

DOM selectors are derived from a live capture of /membership/march-2026
on 2026-05-04 — see ``tests/fixtures/choice_claim/`` and
``humble-diagnose/diagnose-*/safe-to-share/membership/01-initial.html``.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from humble_bundle_keys.models import ExtractStats, GameKey

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slug derivation
# ---------------------------------------------------------------------------

# An order's product.machine_name typically looks like:
#   march_2026_choice
#   december_2022_choice
#   april_2018_monthly
#   august_2023_choice_storefront
# The membership URL slug is the kebab-case form with the suffix stripped:
#   march-2026 / december-2022 / april-2018 / august-2023
_SUFFIXES = ("_choice_storefront", "_choice", "_monthly")


def derive_membership_slug(product: dict[str, Any]) -> str | None:
    """Return the /membership/<slug> path component, or None if undetermined.

    Prefer the explicit ``choice_url`` if Humble provided it; otherwise
    derive from ``machine_name``.
    """
    if not isinstance(product, dict):
        return None
    explicit = product.get("choice_url")
    if isinstance(explicit, str) and explicit:
        return explicit.strip("/")
    mn = (product.get("machine_name") or "").lower()
    if not mn:
        return None
    for suffix in _SUFFIXES:
        if mn.endswith(suffix):
            mn = mn[: -len(suffix)]
            break
    return mn.replace("_", "-") or None


# ---------------------------------------------------------------------------
# DOM selectors
# ---------------------------------------------------------------------------

SEL = {
    # Game cards on the membership page. ``.claimed`` modifier indicates the
    # game has already been claimed.
    "card": ".content-choice",
    "card_claimed_class": "claimed",
    # Clickable inner element of a card.
    "card_click": ".choice-content.js-open-choice-modal, .choice-content",
    # Title element inside a card.
    "card_title": ".content-choice-title, .content-title",
    # Modal that opens after clicking a card.
    "modal_open": ".humblemodal-modal--open, .choice-modal.humblemodal-wrapper",
    # The "GET GAME ON STEAM" affordance. IMPORTANT: this is NOT a <button>
    # — it's a <div class="js-keyfield keyfield enabled" title="Get game on
    # Steam"> styled to look button-shaped, with a child <div class=
    # "keyfield-value">Get game on Steam</div> as the visible label. After
    # a successful claim, the parent div gains a ``redeemed`` class and the
    # child's text changes to the actual Steam key.
    #
    # We deliberately exclude ``.redeemed`` to avoid clicking already-claimed
    # entries, and we exclude ``.giftfield`` (the sibling "Gift to friend on
    # Steam" affordance, which we never want to trigger).
    "get_game_button": (
        ".js-keyfield.keyfield.enabled:not(.redeemed):not(.expired), "
        ".js-keyfield.keyfield:not(.redeemed):not(.expired):not(.giftfield), "
        "[title='Get game on Steam']:not(.redeemed):not(.expired)"
    ),
    # Indicator that this key has expired upstream. Modal still appears with
    # a styled-as-button div and text like "This key has expired and can no
    # longer be redeemed" — clicking does nothing and we'd waste the full
    # timeout. We detect it pre-click and skip.
    "expired_keyfield": ".js-keyfield.keyfield.expired, .js-keyfield.expired",
    # The key value field is the same ``.keyfield-value`` div both before
    # and after the claim — but only its post-claim content is a real key.
    # We scope to the .redeemed parent so we never read the pre-claim
    # placeholder ("Get game on Steam") as a key.
    "key_field": (
        ".js-keyfield.keyfield.redeemed .keyfield-value, "
        ".js-keyfield.redeemed .keyfield-value"
    ),
    # Close modal button.
    "modal_close": ".js-close-modal, [aria-label='Close'], .close-modal",
    # Claimed banner that appears at top of modal post-claim.
    "claimed_banner": ".claimed-banner.visible, .claimed-badge.visible",
}

# A revealed Steam-style key — used as a sanity check on extraction.
KEY_PATTERN = re.compile(r"\b[A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8}){1,4}\b")


# ---------------------------------------------------------------------------
# Options + result types
# ---------------------------------------------------------------------------

@dataclass
class BrowserClaimOptions:
    dry_run: bool = False
    polite_delay_s: float = 3.0
    max_claims: int = 25
    # If set, only walk this membership slug (e.g. 'june-2025'). Otherwise
    # walk every Choice/Monthly order discovered.
    only_slug: str | None = None
    page_timeout_ms: int = 30_000
    # Time to wait for the modal-key field to populate after the click.
    # Humble's backend allocates a key in 3-10 s typically, but some titles
    # take longer (we've seen 15-20 s on subscription/trial content).
    # 30 s gives generous headroom without leaving the user staring forever
    # at an expired-key placeholder (which we now detect and skip pre-click).
    key_wait_ms: int = 30_000


@dataclass
class BrowserClaimAttempt:
    slug: str
    title: str
    success: bool = False
    already_claimed: bool = False
    key_exhausted: bool = False
    key: str = ""
    error: str = ""


@dataclass
class BrowserClaimResult:
    attempts: list[BrowserClaimAttempt] = field(default_factory=list)
    revealed_keys: list[GameKey] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Claimer
# ---------------------------------------------------------------------------

class BrowserChoiceClaimer:
    def __init__(self, context: BrowserContext, options: BrowserClaimOptions):
        self.context = context
        self.options = options
        self.stats = ExtractStats()
        self._page: Page | None = None

    def _get_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            self._page = self.context.new_page()
            self._page.set_default_timeout(self.options.page_timeout_ms)
        return self._page

    def _close_page(self) -> None:
        if self._page is not None and not self._page.is_closed():
            try:
                self._page.close()
            except Exception:
                pass
        self._page = None

    # ------------------------------------------------------------------
    # Top-level driver
    # ------------------------------------------------------------------

    def claim_all(self, orders: list[dict[str, Any]]) -> BrowserClaimResult:
        try:
            return self._claim_all_inner(orders)
        finally:
            self._close_page()

    def _claim_all_inner(self, orders: list[dict[str, Any]]) -> BrowserClaimResult:
        result = BrowserClaimResult()
        targets = self._discover_targets(orders)
        if self.options.only_slug:
            wanted = self.options.only_slug.strip("/")
            targets = [t for t in targets if t[1] == wanted]
            if not targets:
                log.warning(
                    "--membership-only %r matched no order. Available slugs: %s",
                    wanted,
                    ", ".join(sorted({s for _o, s in self._discover_targets(orders)})),
                )
                return result

        log.info(
            "Browser claim: %d membership page(s) to visit (cap %d claims, "
            "%.1fs delay between clicks).",
            len(targets),
            self.options.max_claims,
            self.options.polite_delay_s,
        )

        claims_made = 0
        for order, slug in targets:
            if claims_made >= self.options.max_claims:
                log.info("Hit --max-claims=%d. Stopping.", self.options.max_claims)
                break
            page_attempts = self._claim_one_membership(slug, order, claims_made)
            for attempt in page_attempts:
                result.attempts.append(attempt)
                if attempt.success and attempt.key:
                    claims_made += 1
                    self.stats.keys_revealed += 1
                    # Build a GameKey row so the run's CSV picks it up.
                    product = order.get("product") or {}
                    result.revealed_keys.append(
                        GameKey(
                            game_title=attempt.title,
                            platform="steam",  # Get Game on Steam = Steam keys
                            key=attempt.key,
                            bundle_name=product.get("human_name") or "",
                            bundle_date="",
                            redemption_deadline="",
                            redeemed_on_humble=True,
                            os_support="",
                            humble_url=(
                                f"https://www.humblebundle.com/membership/{slug}"
                            ),
                        )
                    )
                if not attempt.success and attempt.error:
                    self.stats.errors.append(
                        f"{attempt.slug} :: {attempt.title!r}: {attempt.error}"
                    )
                if claims_made >= self.options.max_claims:
                    break
        return result

    # ------------------------------------------------------------------
    # Membership-page walking
    # ------------------------------------------------------------------

    def _discover_targets(
        self, orders: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], str]]:
        """Pick orders that have a membership page worth visiting.

        Returns ``(order, slug)`` tuples. We include any subscription order
        with a derivable slug, regardless of whether its tpks look fully
        claimed — the membership page is the source of truth for what's
        actually unclaimed (some games never appear in tpks until clicked).
        """
        out: list[tuple[dict[str, Any], str]] = []
        seen: set[str] = set()
        for order in orders:
            product = order.get("product") or {}
            if product.get("category") != "subscriptioncontent":
                continue
            slug = derive_membership_slug(product)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            out.append((order, slug))
        return out

    def _claim_one_membership(
        self, slug: str, order: dict[str, Any], already_claimed_this_run: int
    ) -> list[BrowserClaimAttempt]:
        attempts: list[BrowserClaimAttempt] = []
        url = f"https://www.humblebundle.com/membership/{slug}"
        page = self._get_page()
        log.info("Visiting %s", url)
        try:
            page.goto(url, wait_until="networkidle", timeout=self.options.page_timeout_ms)
        except PlaywrightTimeoutError:
            log.warning("Membership page %s did not reach networkidle in time", url)
        except Exception as e:
            log.warning("Failed to load %s: %s", url, e)
            return attempts

        cards = page.locator(SEL["card"])
        n_total = cards.count()
        if n_total == 0:
            log.info("  No cards found on %s — empty / wrong page / not subscribed?", url)
            return attempts

        # Snapshot which indices are unclaimed BEFORE we start clicking; the
        # DOM mutates as we claim, so we don't trust live indices later.
        unclaimed_indices: list[int] = []
        titles: list[str] = []
        for i in range(n_total):
            card = cards.nth(i)
            klass = (card.get_attribute("class") or "")
            title = self._read_title(card)
            titles.append(title)
            if SEL["card_claimed_class"] not in klass.split():
                unclaimed_indices.append(i)

        log.info(
            "  %d total cards, %d unclaimed, %d already-claimed",
            n_total,
            len(unclaimed_indices),
            n_total - len(unclaimed_indices),
        )
        if not unclaimed_indices:
            return attempts

        local_claims = 0
        budget = self.options.max_claims - already_claimed_this_run
        for idx in unclaimed_indices:
            if local_claims >= budget:
                break
            title = titles[idx] or "(untitled)"
            attempt = BrowserClaimAttempt(slug=slug, title=title)
            try:
                self._claim_single_card(page, idx, attempt)
            except Exception as e:
                attempt.error = f"unhandled exception: {e}"
                log.warning("  Claim threw: %s", e)
            attempts.append(attempt)
            if attempt.success and attempt.key:
                local_claims += 1
            # Polite delay between cards regardless of outcome.
            if not self.options.dry_run:
                time.sleep(self.options.polite_delay_s)
        return attempts

    def _claim_single_card(
        self, page: Page, card_index: int, attempt: BrowserClaimAttempt
    ) -> None:
        log.info("  Claiming %r…", attempt.title)
        if self.options.dry_run:
            attempt.error = "dry-run; no clicks"
            log.info("    [dry-run] would click card %d", card_index)
            return

        # Re-locate the card freshly each time (DOM may have re-rendered).
        card = page.locator(SEL["card"]).nth(card_index)

        # Click the inner clickable element to open the modal.
        clickable = card.locator(SEL["card_click"]).first
        try:
            clickable.click(timeout=10_000)
        except Exception as e:
            attempt.error = f"card click failed: {e}"
            log.warning("    Failed to click card: %s", e)
            return
        log.info("    → Card clicked, waiting for modal…")

        # Wait for the modal to be visible.
        try:
            page.wait_for_selector(
                SEL["modal_open"], state="visible", timeout=10_000
            )
        except PlaywrightTimeoutError:
            attempt.error = "modal didn't open"
            log.warning("    Modal didn't open within 10s")
            return

        modal = page.locator(SEL["modal_open"]).first

        # Inspect delivery controls, not game descriptions which may mention Epic.
        epic_link_required = modal.evaluate("""e => {
            const labels = Array.from(e.querySelectorAll(
                '.js-keyfield, button, a, [role="button"]'))
                .filter(x => x.getClientRects().length)
                .map(x => x.textContent.trim().toLowerCase());
            const epic = labels.some(t => /epic/.test(t) &&
                /link|connect|account|redeem|关联|绑定|帐户|账户|领取/.test(t));
            const steam = labels.some(t => /get game on steam|reveal.*steam.*key/.test(t));
            return epic && !steam;
        }""")
        if epic_link_required:
            attempt.error = "epic account linking required"
            self._close_modal(page)
            return

        exhausted_msg = self._check_key_exhausted(modal)
        if exhausted_msg:
            attempt.error = f"key exhausted: {exhausted_msg}"
            attempt.key_exhausted = True
            self._close_modal(page)
            return

        # Pre-click check: is this key expired? Modal renders a styled-as-button
        # div with text "This key has expired and can no longer be redeemed"
        # for upstream-expired entries (subscription trials etc.). Clicking
        # it does nothing — we'd burn the full timeout for no reason.
        try:
            expired_el = modal.locator(SEL["expired_keyfield"]).first
            if expired_el.count() and expired_el.is_visible():
                attempt.error = "key expired upstream"
                attempt.already_claimed = True  # bookkeep as "no work to do"
                log.info("    → Key already expired upstream; skipping")
                self._close_modal(page)
                return
        except Exception:
            pass

        # Find and click the "Get Game on Steam" button.
        steam_btn = modal.locator(SEL["get_game_button"]).first
        # Diagnostic: log what we're about to click. Helps catch cases where
        # the selector matched something weird (modal wrapper, etc.) that
        # would close the modal on click instead of triggering the claim.
        try:
            tag = steam_btn.evaluate("el => el.tagName.toLowerCase()", timeout=2_000)
            klass = steam_btn.get_attribute("class") or ""
            log.debug("    click target: <%s class=%r>", tag, klass[:80])
        except Exception:
            tag = "?"
            klass = ""
        if steam_btn.count() == 0:
            # Maybe this card's already-claimed state slipped past our snapshot.
            if modal.locator(SEL["claimed_banner"]).count():
                attempt.already_claimed = True
                attempt.success = True
                attempt.error = "already claimed"
                log.info("    → Already claimed (modal showed claimed banner)")
                # Still try to extract the key
                attempt.key = self._extract_key(modal)
                if attempt.key:
                    log.info("    → Existing key extracted: %s", attempt.key)
                self._close_modal(page)
                return
            attempt.error = "'Get Game on Steam' button not found"
            log.warning("    No 'Get Game on Steam' button visible")
            self._close_modal(page)
            return

        try:
            steam_btn.click(timeout=10_000)
        except Exception as e:
            attempt.error = f"Get-Game-on-Steam click failed: {e}"
            log.warning("    Get-Game-on-Steam click failed: %s", e)
            self._close_modal(page)
            return
        log.info("    → 'GET GAME ON STEAM' clicked, waiting for key…")

        # Wait for the claim to complete inside THIS modal. We can't use a
        # page-scoped wait here because already-claimed cards on the same
        # membership page already have ``.keyfield-value`` elements in the
        # DOM, which would cause page-scoped waits to return instantly.
        #
        # Simple polling: every second, try to extract a key from the modal.
        # Our key_field selector requires a ``.redeemed`` parent class, so
        # extraction returns empty until the claim actually completes. We
        # don't shortcut on the "claimed" banner appearing — empirically
        # that signal fires ~1 s after the click, but Humble's backend
        # often takes another 5-15 s to write the actual key.
        key_text = ""
        deadline = time.time() + (self.options.key_wait_ms / 1000.0)
        last_err = ""
        poll_count = 0
        while time.time() < deadline:
            try:
                key_text = self._extract_key(modal)
            except Exception as e:
                last_err = str(e)
                key_text = ""
            if key_text:
                break
            # Check for "no keys available" or similar exhaustion messages.
            # Humble shows these when the key pool for a game is depleted.
            # Detecting early saves the remaining timeout (~20-25s).
            exhausted_msg = self._check_key_exhausted(modal)
            if exhausted_msg:
                attempt.error = f"key exhausted: {exhausted_msg}"
                attempt.key_exhausted = True
                log.warning("    Key pool exhausted: %s", exhausted_msg)
                self._close_modal(page)
                return
            poll_count += 1
            if poll_count % 5 == 0:
                # Show progress every 5 s so the user knows we're still waiting.
                remaining = int(deadline - time.time())
                log.debug("    still waiting for key… (~%ds left)", max(0, remaining))
            time.sleep(1.0)

        if not key_text:
            attempt.error = (
                f"key field never populated (waited "
                f"{self.options.key_wait_ms} ms){'; ' + last_err if last_err else ''}"
            )
            log.warning(
                "    Key field never populated within %d s timeout",
                int(self.options.key_wait_ms / 1000),
            )

        if key_text:
            attempt.success = True
            attempt.key = key_text
            log.info("    → Key revealed: %s", key_text)
        elif not attempt.error:
            attempt.error = "no key extracted"
            log.warning("    No key extracted from modal")

        self._close_modal(page)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_title(self, card: Locator) -> str:
        for sel in SEL["card_title"].split(", "):
            try:
                el = card.locator(sel).first
                if el.count():
                    text = el.inner_text(timeout=1_500).strip()
                    if text:
                        return text
            except Exception:
                continue
        return ""

    def _extract_key(self, modal: Locator) -> str:
        """Pull the revealed Steam key out of the modal.

        Strict: only returns a string that matches KEY_PATTERN. Earlier
        versions had a "return any short text" fallback that ended up
        capturing the button label ("GET GAME ON STEAM") as the key when
        the click hit the wrong element and the keyfield never populated.

        Belt-and-suspenders: reject any string that matches the pre-claim
        placeholder text Humble puts in the same .keyfield-value element.
        """
        placeholders = {
            "get game on steam",
            "gift to friend on steam",
        }
        for sel in SEL["key_field"].split(", "):
            try:
                el = modal.locator(sel).first
                if not el.count():
                    continue
                # If it's an input, try .value
                try:
                    val = el.input_value(timeout=500)
                    if val and val.strip().lower() not in placeholders:
                        m = KEY_PATTERN.search(val.upper())
                        if m:
                            return m.group(0)
                except Exception:
                    pass
                # Otherwise read inner text
                try:
                    txt = el.inner_text(timeout=500).strip()
                except Exception:
                    txt = ""
                if txt and txt.lower() not in placeholders:
                    m = KEY_PATTERN.search(txt.upper())
                    if m:
                        return m.group(0)
            except Exception:
                continue
        return ""

    def _check_key_exhausted(self, modal: Locator) -> str:
        """Check if the modal shows a 'no keys available' message.

        Humble displays messages like "There are no more keys available for
        this title at this time" when the key pool is depleted. Detecting
        this early saves the remaining 20-25s of the timeout.

        Returns the matched message text if exhausted, empty string otherwise.
        """
        exhaustion_phrases = (
            "keys are temporarily exhausted",
            "keys temporarily exhausted",
            "keys are exhausted",
            "密钥暂时缺货",
            "no more keys available",
            "no keys available",
            "key unavailable",
            "currently unavailable",
            "out of stock",
            "no longer available",
        )
        try:
            text = modal.inner_text(timeout=500).lower()
            for phrase in exhaustion_phrases:
                if phrase in text:
                    return phrase
        except Exception:
            pass
        return ""

    def _close_modal(self, page: Page) -> None:
        try:
            close = page.locator(SEL["modal_close"]).first
            if close.count():
                close.click(timeout=2_000)
                page.wait_for_timeout(300)
                return
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass
