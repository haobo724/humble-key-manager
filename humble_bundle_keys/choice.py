"""Humble Choice claim flow.

Closes the gap from `humble_bundle_keys.api` for Humble Choice subscription
content. Standard non-Choice keys can be revealed by hitting
`/humbler/redeemkey` directly; Choice content must first be "chosen"
via `/humbler/choosecontent` and only then revealed.

The contract was captured on 2026-05-04 — see
``docs/CHOICE_CLAIM_SPEC.md`` for the live wire trace and reasoning.

Two-step flow per game::

    POST /humbler/choosecontent
      gamekey=<order_gamekey>
      parent_identifier=initial
      chosen_identifiers[]=<short_id>      ← machine_name minus suffix
    → {"success": true, "force_refresh": true}

    POST /humbler/redeemkey                 ← same endpoint as Flow A
      keytype=<machine_name>                  e.g. zerohour_row_choice_steam
      key=<order_gamekey>
      keyindex=<idx>
    → {"success": true, "key": "XXXXX-XXXXX-XXXXX"}

Safety: this module mutates state on Humble's side. Each successful
``claim_one`` consumes a Choice slot for the user. The caller is
responsible for opt-in confirmation (the CLI does this behind
``--claim-choice``) and for capping the number of claims per run.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from playwright.sync_api import APIRequestContext, BrowserContext, Page

from humble_bundle_keys._browser_fetch import post_form_in_browser
from humble_bundle_keys.api import _extract_tpk
from humble_bundle_keys.models import ExtractStats, GameKey

# Anchor page used to route POSTs through real Chrome (bypasses Cloudflare's
# bot detection on Playwright's APIRequestContext). Same anchor as ApiScraper.
ANCHOR_URL = "https://www.humblebundle.com/home/keys"

CHOOSECONTENT_URL = "https://www.humblebundle.com/humbler/choosecontent"
REDEEMKEY_URL = "https://www.humblebundle.com/humbler/redeemkey"

# A tpk's machine_name for a Choice game has many real-world shapes — we've
# observed all of these in production:
#
#   chantsofsennaar_choice_steam              (no regional modifier)
#   etrianodysseyiiihd_row_choice_steam       (with `_row_` modifier)
#   tempestrising_naeu_choice_steam           (regional `_naeu_`)
#   diplomacyisnotanoption_choice_epic_keyless (multi-word platform)
#
# The "short identifier" sent to /humbler/choosecontent is everything before
# the trailing `_<modifier>?_choice_<platform...>` suffix. The modifier (if
# present) is a single word like `row` or `naeu`.
# Note: ``c?hoice`` matches both ``choice`` and Humble's real-world typo
# ``hoice`` — we've seen e.g. ``road96_europe_hoice_steam`` in production
# data inside an order whose own machine_name is ``august_2023_choice``.
_CHOICE_KEYTYPE_RE = re.compile(
    r"^(?P<short>.+?)_(?:[a-z]+_)?c?hoice_(?P<platform>[a-z][a-z_]*)$"
)

# Heuristic for "this order is a Humble Choice subscription month": we look
# at ``order.product.machine_name`` which has shapes like:
#
#   march_2026_choice            (modern Choice)
#   december_2022_choice         (older modern Choice)
#   march_2026_choice_storefront (also seen in some captures)
#
# We deliberately do NOT match `*_monthly` orders here — those are the
# legacy Humble Monthly subscription, which uses Flow A (direct redeemkey)
# rather than the two-step choosecontent + redeemkey flow.
_CHOICE_ORDER_RE = re.compile(r"_choice(?:_storefront|_v\d+)?$")

log = logging.getLogger(__name__)


@dataclass
class ChoiceOptions:
    dry_run: bool = False
    polite_delay_s: float = 3.0  # matches observed UI lag of 3-10 s
    max_claims: int = 25  # hard cap per run
    parent_identifier: str = "initial"  # only value seen in current Choice


class ChoiceError(RuntimeError):
    """Raised on any non-2xx or unexpected-shape response from Choice endpoints."""


@dataclass
class _ClaimAttempt:
    title: str
    short_id: str
    keytype: str
    keyindex: int
    gamekey: str
    success: bool = False
    key: str = ""
    error: str = ""


@dataclass
class ChoiceClaimResult:
    attempts: list[_ClaimAttempt] = field(default_factory=list)
    revealed_keys: list[GameKey] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure helpers (no network — covered by tests/test_choice.py fixtures)
# ---------------------------------------------------------------------------


def is_choice_keytype(machine_name: str | None) -> bool:
    """True if this tpk's machine_name is a Choice content row."""
    if not machine_name:
        return False
    return bool(_CHOICE_KEYTYPE_RE.match(machine_name))


def categorize_keytype(machine_name: str | None) -> str:
    """Best-effort category for a tpk machine_name.

    Used to give users a clear picture of *why* a tpk didn't yield a key.
    Categories:
      - 'choice'         — Humble Choice / Monthly Choice; needs --claim-choice
      - 'monthly'        — Legacy Humble Monthly (pre-Choice); usually
                           claimable via direct redeemkey
      - 'freegame'       — Free-game promo claims (different mechanism)
      - 'keyless'        — Epic Games "keyless" delivery — no key exists
      - 'softwarebundle' — Audio / software vendor bundles with their own flow
      - 'voucher'        — Store credit voucher, not a Steam-shaped key
      - 'bundle'         — Regular bundle key (should work via direct redeemkey)
      - 'other'          — Anything we don't recognize
    """
    if not machine_name:
        return "other"
    mn = machine_name.lower()
    if "_keyless" in mn:
        return "keyless"
    if "voucher" in mn or "_giftcard" in mn or "_coupon" in mn:
        return "voucher"
    if "_softwarebundle" in mn:
        return "softwarebundle"
    if "_freegame" in mn:
        return "freegame"
    if is_choice_keytype(mn):
        return "choice"
    if "_monthly_" in mn or mn.endswith("_monthly"):
        return "monthly"
    if "_bundle_" in mn or mn.endswith("_bundle"):
        return "bundle"
    return "other"


def short_id_for_keytype(machine_name: str) -> str | None:
    """Extract the short identifier ChoiceContent expects.

    >>> short_id_for_keytype("zerohour_row_choice_steam")
    'zerohour'
    >>> short_id_for_keytype("plain_old_steam") is None
    True
    """
    m = _CHOICE_KEYTYPE_RE.match(machine_name)
    if not m:
        return None
    return m.group("short")


def looks_like_choice_order(order: dict[str, Any]) -> bool:
    """True if this order is a Humble Choice subscription month.

    Note: order metadata lives under ``order.product``, NOT at the root.
    The root has fields like ``gamekey``, ``claimed``, ``choices_remaining``;
    ``machine_name`` and ``human_name`` are nested under ``product``.
    """
    product = order.get("product") or {}
    # Strongest signal: subscription content with a "choice"-shaped name.
    if product.get("category") == "subscriptioncontent":
        machine_name = (product.get("machine_name") or "").lower()
        if _CHOICE_ORDER_RE.search(machine_name):
            return True
        # Some Choice orders may have machine_name that doesn't match the
        # regex (legacy formats). Fall back to the human-readable name.
        human = (product.get("human_name") or "").lower()
        if "humble choice" in human:
            return True
    # Be tolerant of un-nested orders (older API shapes).
    machine_name = (order.get("machine_name") or "").lower()
    if _CHOICE_ORDER_RE.search(machine_name):
        return True
    human = (order.get("human_name") or "").lower()
    return "humble choice" in human


def unclaimed_choice_tpks(order: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the tpk entries in this order that are unclaimed Choice games."""
    tpkd = order.get("tpkd_dict") or {}
    out = []
    for arr_key in ("all_tpks", "chosen_tpks", "primary_tpks"):
        arr = tpkd.get(arr_key)
        if not isinstance(arr, list):
            continue
        for tpk in arr:
            if not isinstance(tpk, dict):
                continue
            if tpk.get("redeemed_key_val"):
                continue
            mn = tpk.get("machine_name")
            if not is_choice_keytype(mn):
                continue
            out.append(tpk)
        if out:
            return out  # prefer first non-empty array
    return out


def build_choosecontent_body(
    gamekey: str, short_ids: list[str], parent_identifier: str = "initial"
) -> str:
    """Produce the form-encoded body for /humbler/choosecontent.

    Multiple short_ids result in repeated ``chosen_identifiers[]=...`` params
    — exactly the shape the SPA sends.
    """
    pairs: list[tuple[str, str]] = [
        ("gamekey", gamekey),
        ("parent_identifier", parent_identifier),
    ]
    for sid in short_ids:
        pairs.append(("chosen_identifiers[]", sid))
    return urlencode(pairs)


def build_redeemkey_body(gamekey: str, keytype: str, keyindex: int) -> str:
    """Produce the form-encoded body for /humbler/redeemkey."""
    return urlencode(
        [
            ("keytype", keytype),
            ("key", gamekey),
            ("keyindex", str(keyindex)),
        ]
    )


def extract_revealed_key(body: Any) -> str | None:
    """Pick the revealed key out of a /humbler/redeemkey response body."""
    if not isinstance(body, dict):
        return None
    for k in ("key", "redeemed_key_val", "key_val"):
        v = body.get(k)
        if isinstance(v, str) and v:
            return v
    return None


# ---------------------------------------------------------------------------
# Network layer
# ---------------------------------------------------------------------------


def _csrf_token_from_context(context: BrowserContext) -> str | None:
    for c in context.cookies("https://www.humblebundle.com"):
        if c.get("name") == "csrf_cookie":
            return c.get("value")
    return None


class ChoiceClaimer:
    """Drive the two-step Choice claim flow over an authenticated context."""

    def __init__(self, context: BrowserContext, options: ChoiceOptions):
        self.context = context
        self.options = options
        self.request: APIRequestContext = context.request
        self.stats = ExtractStats()
        self._anchor_page: Page | None = None

    def _get_anchor_page(self) -> Page:
        """Lazy-create / heal a Page navigated to humblebundle.com.

        Same heal logic as ApiScraper — protects against the page getting
        closed or drifting off-origin (which causes
        ``Execution context was destroyed`` on the next evaluate).
        """
        page = self._anchor_page
        if page is None or page.is_closed():
            page = self.context.new_page()
            page.goto(ANCHOR_URL, wait_until="domcontentloaded")
            self._anchor_page = page
            return page

        try:
            current = page.url or ""
        except Exception:
            current = ""
        if "humblebundle.com" not in current:
            try:
                page.goto(ANCHOR_URL, wait_until="domcontentloaded")
            except Exception:
                try:
                    page.close()
                except Exception:
                    pass
                page = self.context.new_page()
                page.goto(ANCHOR_URL, wait_until="domcontentloaded")
                self._anchor_page = page
        return page

    def close_anchor_page(self) -> None:
        if self._anchor_page is not None and not self._anchor_page.is_closed():
            try:
                self._anchor_page.close()
            except Exception:
                pass
        self._anchor_page = None

    # --- low-level HTTP ---------------------------------------------------

    def _post_form(self, url: str, body: str, *, referer: str) -> dict[str, Any]:
        # Route through real Chrome to bypass Cloudflare's bot detection on
        # Playwright's APIRequestContext.
        extra: dict[str, str] = {}
        token = _csrf_token_from_context(self.context)
        if token:
            extra["CSRF-Prevention-Token"] = token
        try:
            page = self._get_anchor_page()
            resp = post_form_in_browser(
                page,
                url,
                body,
                referer=referer,
                extra_headers=extra,
                timeout_ms=20_000,
            )
        except Exception as e:
            raise ChoiceError(f"POST {url} failed: {e}") from e
        if resp.status >= 400:
            detail = (resp.raw_text or "")[:200]
            raise ChoiceError(f"POST {url} returned {resp.status} {detail!r}")
        if resp.body is None:
            raise ChoiceError(f"POST {url} returned non-JSON body: {resp.raw_text[:200]!r}")
        return resp.body

    # --- per-game flow ---------------------------------------------------

    def claim_one(
        self, order: dict[str, Any], tpk: dict[str, Any]
    ) -> _ClaimAttempt:
        """Run choosecontent + redeemkey for a single tpk; return the attempt."""
        machine_name = tpk.get("machine_name") or ""
        short_id = short_id_for_keytype(machine_name)
        gamekey = order.get("gamekey") or ""
        # Humble uses `keyindex` (one word) on order tpks. Tolerate `key_index`
        # in case of future shape changes.
        keyindex = int(tpk.get("keyindex", tpk.get("key_index", 0)) or 0)
        title = tpk.get("human_name") or machine_name or "(unknown)"

        attempt = _ClaimAttempt(
            title=title,
            short_id=short_id or "",
            keytype=machine_name,
            keyindex=keyindex,
            gamekey=gamekey,
        )

        if not (short_id and gamekey):
            attempt.error = "missing short_id or gamekey"
            return attempt

        if self.options.dry_run:
            attempt.error = "dry-run; no calls made"
            return attempt

        month_slug = (order.get("machine_name") or "").replace("_storefront", "")
        referer = f"https://www.humblebundle.com/membership/{month_slug.replace('_', '-')}"

        # Step 1
        body1 = build_choosecontent_body(
            gamekey=gamekey,
            short_ids=[short_id],
            parent_identifier=self.options.parent_identifier,
        )
        try:
            resp1 = self._post_form(CHOOSECONTENT_URL, body1, referer=referer)
        except ChoiceError as e:
            attempt.error = f"choosecontent: {e}"
            return attempt
        if not (isinstance(resp1, dict) and resp1.get("success")):
            attempt.error = f"choosecontent unexpected: {resp1!r}"
            return attempt

        # Step 2
        body2 = build_redeemkey_body(
            gamekey=gamekey, keytype=machine_name, keyindex=keyindex
        )
        try:
            resp2 = self._post_form(REDEEMKEY_URL, body2, referer=referer)
        except ChoiceError as e:
            attempt.error = f"redeemkey: {e}"
            return attempt
        key = extract_revealed_key(resp2)
        if not key:
            attempt.error = f"redeemkey returned no key: {resp2!r}"
            return attempt

        attempt.success = True
        attempt.key = key
        return attempt

    # --- top-level driver ------------------------------------------------

    def claim_all(self, orders: list[dict[str, Any]]) -> ChoiceClaimResult:
        """Walk every Choice order, claim every unclaimed Choice tpk."""
        try:
            return self._claim_all_inner(orders)
        finally:
            self.close_anchor_page()

    def _claim_all_inner(self, orders: list[dict[str, Any]]) -> ChoiceClaimResult:
        result = ChoiceClaimResult()
        claims_made = 0
        for order in orders:
            if not looks_like_choice_order(order):
                continue
            tpks = unclaimed_choice_tpks(order)
            if not tpks:
                continue
            for tpk in tpks:
                if claims_made >= self.options.max_claims:
                    log.warning(
                        "Hit --max-claims=%d; stopping (%d remaining tpks "
                        "across the rest of this order will not be claimed).",
                        self.options.max_claims,
                        len(tpks) - tpks.index(tpk),
                    )
                    return result

                attempt = self.claim_one(order, tpk)
                result.attempts.append(attempt)
                if attempt.success:
                    self.stats.keys_revealed += 1
                    # Build a GameKey row out of the now-revealed tpk so the
                    # caller can merge it into the CSV.
                    tpk_with_key = dict(tpk)
                    tpk_with_key["redeemed_key_val"] = attempt.key
                    result.revealed_keys.append(_extract_tpk(tpk_with_key, order))
                    claims_made += 1
                    log.info("Claimed %s (%s)", attempt.title, attempt.short_id)
                else:
                    self.stats.errors.append(
                        f"{attempt.title}: {attempt.error}"
                    )
                    log.warning("Failed to claim %s: %s", attempt.title, attempt.error)

                if claims_made > 0 and not self.options.dry_run:
                    time.sleep(self.options.polite_delay_s)
        return result
