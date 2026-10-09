"""JSON API scraper for humblebundle.com.

Humble has no public API, but their frontend SPA talks to a private JSON API
on the same domain that has been stable enough for community tools to use it
for years. This module hits those endpoints with the same authenticated
session that the DOM scraper uses.

Endpoints (all undocumented; subject to change without notice)
--------------------------------------------------------------
* ``GET  /api/v1/user/order``                 — list of order ``gamekey``s.
* ``GET  /api/v1/order/<gamekey>?all_tpkds=true``
                                              — full order detail incl. keys.
* ``POST /humbler/redeemkey``                 — reveals + claims one key.

Fields we read from each TPK entry (best-effort; see ``_extract_tpk``)
---------------------------------------------------------------------
* ``human_name``               — game title.
* ``key_type`` / ``key_type_human_name`` — platform.
* ``redeemed_key_val``         — actual key, or null if not yet revealed.
* ``is_expired``, ``expiry_date`` — redemption deadline.
* ``machine_name``             — used as the ``keytype`` form field when
                                  POSTing to /humbler/redeemkey.
* ``key_index``                — used as the ``keyindex`` form field.
* ``gamekey``                  — used as the ``key`` form field.

If the API returns a shape we don't recognise, we raise ``ApiUnsupported``
which the orchestrator uses to fall back to DOM scraping.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from playwright.sync_api import APIRequestContext, BrowserContext, Page

from humble_bundle_keys._browser_fetch import post_form_in_browser
from humble_bundle_keys._orders_cache import OrderCache
from humble_bundle_keys.diagnostics import emit_failure, extracting, order_ref, set_context
from humble_bundle_keys.models import ExtractStats, GameKey
from humble_bundle_keys.regions import countries
from humble_bundle_keys.scraper import PLATFORMS  # reuse the platform normalisation

# Page we keep open as a "browser context anchor" so POSTs can route through
# the real Chrome stack (and bypass Cloudflare's bot detection on Playwright's
# raw APIRequestContext). Any humblebundle.com URL works — /home/keys is the
# obvious choice since we know it loads cleanly.
ANCHOR_URL = "https://www.humblebundle.com/home/keys"

ORDERS_LIST_URL = "https://www.humblebundle.com/api/v1/user/order"
ORDER_DETAIL_URL = "https://www.humblebundle.com/api/v1/order/{gamekey}?all_tpkds=true"
REDEEM_URL = "https://www.humblebundle.com/humbler/redeemkey"

# Keytype categories that are structurally not auto-redeemable via
# /humbler/redeemkey. Pre-skipping these avoids wasted round-trips.
_SKIP_CATEGORIES = frozenset({"softwarebundle", "voucher", "keyless", "freegame"})

log = logging.getLogger(__name__)


class ApiUnsupported(RuntimeError):
    """Raised when the JSON API returns a shape we can't parse.

    The orchestrator catches this and falls back to DOM scraping.
    """


class ApiError(RuntimeError):
    """Raised on an authoritative API failure (non-2xx, missing auth)."""


@dataclass
class ApiOptions:
    reveal_keys: bool = True
    dry_run: bool = False
    polite_delay_ms: int = 800
    request_timeout_ms: int = 20_000
    # Cache control. When ``cache`` is None, no caching is done.
    cache: OrderCache | None = None


def _normalise_platform(s: str | None) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    return PLATFORMS.get(s, s if s.isalpha() and len(s) <= 16 else "")


def _expiry_to_deadline(tpk: dict[str, Any]) -> str:
    """Convert API expiry fields to a human-readable deadline string."""
    if tpk.get("is_expired"):
        return "Expired"
    expiry = tpk.get("expiry_date") or tpk.get("expires") or ""
    if isinstance(expiry, str) and expiry:
        return f"Must be redeemed by {expiry}"
    return ""


def normalise_key(value: Any) -> str:
    """Accept only a text key or an unambiguous named key wrapper.

    Never stringify an opaque response object into an activation code.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        candidates = {value[name] for name in ("key", "key_val", "redeemed_key_val")
                      if isinstance(value.get(name), str) and value[name]}
        if len(candidates) == 1:
            return candidates.pop()
    raise ApiUnsupported("密钥字段格式无法识别；该订单未执行刮取，请打开原页面核查。")


@extracting
def _extract_tpk(tpk: dict[str, Any], order: dict[str, Any]) -> GameKey:
    """Build a GameKey from one entry in tpkd_dict.all_tpks[]."""
    title = (
        tpk.get("human_name")
        or tpk.get("display_name")
        or tpk.get("machine_name")
        or "(unknown)"
    )
    platform = _normalise_platform(tpk.get("key_type") or tpk.get("key_type_human_name"))
    key_val = normalise_key(tpk.get("redeemed_key_val"))
    # Order metadata lives under ``order.product``, NOT at the root. The root
    # has fields like gamekey, uid, created. Fall back to root for tolerance
    # against API shape changes.
    product = order.get("product") or {}
    bundle_name = (
        product.get("human_name")
        or product.get("machine_name")
        or order.get("human_name")
        or order.get("machine_name")
        or ""
    )
    deadline = _expiry_to_deadline(tpk)
    gamekey = order.get("gamekey") or tpk.get("gamekey") or ""
    humble_url = f"https://www.humblebundle.com/downloads?key={gamekey}" if gamekey else ""

    # OS support — best effort. Humble doesn't have a clean per-tpk OS field,
    # so we scan instructions_html for OS keywords. Don't bother trying to
    # infer from steam_app_id presence etc. — we'd rather underreport than lie.
    oses: list[str] = []
    instructions = (tpk.get("instructions_html") or "").lower()
    for needle, label in (("windows", "Windows"), ("mac", "macOS"), ("linux", "Linux")):
        if needle in instructions:
            oses.append(label)

    return GameKey(
        game_title=title,
        platform=platform,
        key=key_val,
        bundle_name=bundle_name,
        bundle_date="",  # filled in by exporter from bundle_name
        redemption_deadline=deadline,
        redeemed_on_humble=bool(key_val),
        os_support=", ".join(oses),
        humble_url=humble_url,
        steam_app_id=_steam_app_id(tpk) if platform == "steam" else None,
        exclusive_countries=countries(tpk.get("exclusive_countries")),
        disallowed_countries=countries(tpk.get("disallowed_countries")),
    )


def _steam_app_id(tpk: dict) -> int | None:
    value = tpk.get("steam_app_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str)) and str(value).isdigit() and int(value) > 0:
        return int(value)
    return None


class ApiScraper:
    """Scrape via the private JSON API.

    Uses the same authenticated context the DOM scraper uses; no separate
    auth flow needed. Cookies are shared.
    """

    def __init__(self, context: BrowserContext, options: ApiOptions):
        self.context = context
        self.options = options
        self.request: APIRequestContext = context.request
        self.stats = ExtractStats()
        # Populated as we walk orders so callers (e.g. ChoiceClaimer) can
        # reuse the data without re-fetching.
        self.orders: list[dict[str, Any]] = []
        # Anchor page used to route POSTs through real Chrome's HTTP stack —
        # the only path Cloudflare doesn't 403. Created lazily on first reveal
        # so dry-run / GET-only operation stays cheap.
        self._anchor_page: Page | None = None

    def _get_anchor_page(self) -> Page:
        """Lazy-create / heal a Page navigated to humblebundle.com.

        The anchor's only job is to be a real-Chrome request origin. If it
        gets closed, navigates away (e.g. an auth-refresh redirect), or
        ends up off-origin, recreate or re-navigate it. Otherwise the next
        ``page.evaluate`` will throw "Execution context was destroyed".
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
                # Fall back to a fresh page
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

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def scrape(self) -> tuple[list[GameKey], ExtractStats]:
        try:
            gamekeys = self._list_gamekeys()
            log.info("API: listing %d orders", len(gamekeys))
            rows: list[GameKey] = []
            for i, gk in enumerate(gamekeys, 1):
                log.info("API: order %d/%d (%s)", i, len(gamekeys), gk[:6] + "…")
                set_context(phase="读取订单", bundle="", game="", order_ref=order_ref(gk))
                try:
                    order = self._get_order(gk)
                except ApiError as e:
                    emit_failure("order_failed", e)
                    self.stats.errors.append(f"order {gk}: {e}")
                    continue

                tpks = self._extract_tpks(order)
                try:
                    parsed = [_extract_tpk(tpk, order) for tpk in tpks]
                except ApiUnsupported as exc:
                    emit_failure("order_parse_failed", exc)
                    self.stats.errors.append("一个订单的密钥字段格式无法识别；未执行刮取。")
                    continue
                self.orders.append(order)
                self.stats.bundles_processed += 1

                for tpk, game in zip(tpks, parsed, strict=True):
                    # Reveal if not already revealed.
                    if (
                        not game.key
                        and self.options.reveal_keys
                        and not self.options.dry_run
                    ):
                        # Pre-skip keytypes that are structurally not
                        # auto-redeemable — saves a wasted round-trip per entry.
                        from humble_bundle_keys.choice import categorize_keytype

                        cat = categorize_keytype(tpk.get("machine_name"))
                        if cat in _SKIP_CATEGORIES:
                            self.stats.skipped_structural += 1
                            self.stats.skipped_structural_tpks.append((
                                str(tpk.get("human_name") or "(unknown)"),
                                str(tpk.get("machine_name") or "(none)"),
                                cat,
                            ))
                            log.debug(
                                "Pre-skipped %r (%s): category %r",
                                tpk.get("human_name"), tpk.get("machine_name"), cat,
                            )
                            rows.append(game)
                            continue

                        revealed = self._reveal(tpk, order)
                        if revealed:
                            game.key = revealed
                            game.redeemed_on_humble = True
                            self.stats.keys_revealed += 1
                            time.sleep(self.options.polite_delay_ms / 1000.0)
                    elif game.key:
                        self.stats.keys_already_revealed += 1
                    rows.append(game)

            self.stats.total_rows = len(rows)
            return rows, self.stats
        finally:
            # Close the anchor page so we don't leak it. The caller still owns
            # the BrowserContext lifecycle.
            self.close_anchor_page()

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def _get_json(self, url: str) -> Any:
        try:
            resp = self.request.get(url, timeout=self.options.request_timeout_ms)
        except Exception as e:
            raise ApiError(f"GET {url} failed: {e}") from e
        if resp.status == 401 or resp.status == 403:
            raise ApiError(f"GET {url} returned {resp.status} — session not authenticated for API")
        if resp.status >= 400:
            raise ApiError(f"GET {url} returned {resp.status}")
        try:
            return resp.json()
        except Exception as e:
            raise ApiUnsupported(f"GET {url} returned non-JSON body: {e}") from e

    def _list_gamekeys(self) -> list[str]:
        body = self._get_json(ORDERS_LIST_URL)
        # Expected: list of {"gamekey": "..."}
        if not isinstance(body, list):
            raise ApiUnsupported(
                f"/api/v1/user/order returned {type(body).__name__}, expected list"
            )
        out = []
        for item in body:
            if isinstance(item, dict) and isinstance(item.get("gamekey"), str):
                out.append(item["gamekey"])
            elif isinstance(item, str):
                out.append(item)
        if not out and body:
            raise ApiUnsupported(
                "/api/v1/user/order returned items in an unrecognised shape; "
                f"first item: {body[0]!r}"
            )
        return out

    def _get_order(self, gamekey: str, *, _count_stats: bool = True) -> dict[str, Any]:
        cache = self.options.cache
        if cache is not None:
            cached = cache.get(gamekey, count_stats=_count_stats)
            if cached is not None:
                return cached
        body = self._get_json(ORDER_DETAIL_URL.format(gamekey=gamekey))
        if not isinstance(body, dict):
            raise ApiUnsupported(
                f"/api/v1/order/{gamekey} returned {type(body).__name__}, expected dict"
            )
        if cache is not None:
            cache.put(gamekey, body)
        return body

    def _extract_tpks(self, order: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull the list of key entries out of an order detail blob."""
        tpkd = order.get("tpkd_dict")
        if not isinstance(tpkd, dict):
            return []
        # Prefer all_tpks; fall back to chosen_tpks/primary_tpks if needed.
        for k in ("all_tpks", "chosen_tpks", "primary_tpks"):
            arr = tpkd.get(k)
            if isinstance(arr, list) and arr:
                return arr
        return []

    # ------------------------------------------------------------------
    # Reveal + claim
    # ------------------------------------------------------------------

    def _csrf_token(self) -> str | None:
        """Pull the CSRF token from the context's cookies."""
        for c in self.context.cookies("https://www.humblebundle.com"):
            if c.get("name") == "csrf_cookie":
                return c.get("value")
        return None

    def _reveal(self, tpk: dict[str, Any], order: dict[str, Any]) -> str | None:
        """POST to /humbler/redeemkey and return the revealed key string, or None."""
        keytype = tpk.get("machine_name")
        # Humble's order JSON uses ``keyindex`` (one word), not ``key_index``.
        # We accept either to be defensive against future shape changes.
        keyindex = tpk.get("keyindex")
        if keyindex is None:
            keyindex = tpk.get("key_index")
        gamekey = order.get("gamekey")
        if not (keytype and gamekey is not None and keyindex is not None):
            log.debug("API: missing fields for reveal: keytype=%r keyindex=%r gamekey=%r",
                     keytype, keyindex, gamekey)
            return None

        form = urlencode(
            [
                ("keytype", str(keytype)),
                ("key", str(gamekey)),
                ("keyindex", str(keyindex)),
            ]
        )
        # Route POST through the real browser to bypass Cloudflare's bot
        # detection on Playwright's APIRequestContext. CSRF token (if any)
        # is added as a header — same as the SPA does it.
        extra_headers: dict[str, str] = {}
        token = self._csrf_token()
        if token:
            extra_headers["CSRF-Prevention-Token"] = token

        # Retry with exponential backoff for transient Cloudflare 403s.
        # A single 403 in an otherwise-successful run is almost always
        # Cloudflare rate-limiting one request; a short pause + retry
        # resolves it.
        max_attempts = 3
        resp = None
        for attempt in range(max_attempts):
            try:
                page = self._get_anchor_page()
                resp = post_form_in_browser(
                    page,
                    REDEEM_URL,
                    form,
                    referer="https://www.humblebundle.com/home/keys",
                    extra_headers=extra_headers,
                    timeout_ms=self.options.request_timeout_ms,
                )
            except Exception as e:
                msg = f"reveal {tpk.get('human_name')!r}: {e}"
                if attempt < max_attempts - 1:
                    log.debug("Reveal attempt %d failed (%s), retrying...", attempt + 1, e)
                    time.sleep(2 ** attempt)
                    continue
                self.stats.errors.append(msg)
                log.warning(msg)
                return None

            if resp.status == 403 and attempt < max_attempts - 1:
                # Transient Cloudflare challenge — back off and retry.
                delay = 2 ** attempt
                log.info(
                    "Reveal %r got 403 (attempt %d/%d), retrying in %ds...",
                    tpk.get("human_name"), attempt + 1, max_attempts, delay,
                )
                time.sleep(delay)
                continue
            break

        if resp is None:
            return None

        if resp.status >= 400:
            detail = (resp.raw_text or "")[:200]
            msg = (
                f"reveal {tpk.get('human_name')!r}: "
                f"status {resp.status} {detail!r}"
            )
            self.stats.errors.append(msg)
            log.warning(msg)
            return None

        try:
            log.debug("reveal %s status=%s", tpk.get("human_name"), resp.status)
        except Exception:
            pass

        def _record_silent() -> None:
            self.stats.keys_silent_no_response += 1
            try:
                product = order.get("product") or {}
                self.stats.silent_no_response_tpks.append(
                    (
                        str(tpk.get("human_name") or "(unknown)"),
                        str(tpk.get("machine_name") or "(none)"),
                        str(product.get("machine_name") or order.get("machine_name") or "(none)"),
                    )
                )
            except Exception:
                pass

        body = resp.body
        if body is None:
            _record_silent()
            return None

        # Possible response shapes:
        #   {"key": "XXXXX-XXXXX-XXXXX"}
        #   {"redeemed_key_val": "XXXXX-XXXXX-XXXXX"}
        #   {"success": true, "key_val": "..."}
        for candidate in ("key", "redeemed_key_val", "key_val"):
            v = body.get(candidate) if isinstance(body, dict) else None
            if isinstance(v, str) and v:
                # Successful reveal — invalidate the order cache so future
                # runs see the populated redeemed_key_val.
                if self.options.cache is not None:
                    self.options.cache.invalidate(str(order.get("gamekey") or ""))
                return v
        # Some endpoints return success without echoing the key — fall back to
        # re-fetching the order to get the now-populated redeemed_key_val.
        try:
            refreshed = self._get_order(str(order.get("gamekey")), _count_stats=False)
            for tpk2 in self._extract_tpks(refreshed):
                tpk2_idx = tpk2.get("keyindex", tpk2.get("key_index"))
                if tpk2.get("machine_name") == keytype and tpk2_idx == keyindex:
                    val = tpk2.get("redeemed_key_val")
                    if isinstance(val, str) and val:
                        return val
        except Exception:
            pass
        # Got a 2xx but couldn't extract a key — almost always means this is
        # Humble Choice content that needs the two-step claim flow.
        _record_silent()
        return None
