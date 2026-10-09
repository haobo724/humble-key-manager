"""Bounded, read-only scanning. No key-reveal or Choice-selection calls."""
import asyncio
import hashlib
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone

from humble_bundle_keys._orders_cache import OrderCache
from humble_bundle_keys.api import ORDER_DETAIL_URL, ORDERS_LIST_URL, ApiError, _extract_tpk
from humble_bundle_keys.browser_choice import SEL
from humble_bundle_keys.diagnostics import error_fields, order_ref, scope


def order_tpks(order):
    data = order.get("tpkd_dict") or {}
    for name in ("all_tpks", "chosen_tpks", "primary_tpks"):
        if isinstance(data.get(name), list) and data[name]:
            return [t for t in data[name] if isinstance(t, dict)]
    return []


class ReadOnlyScanner:
    def __init__(self, context, directory, publish, update, describe, target,
                 previous=None, full=False, log=None, legacy=None):
        self.context, self.publish, self.update = context, publish, update
        self.describe, self.target = describe, target
        self.full = full
        self.log = log or (lambda *args, **kwargs: None)
        self.legacy = legacy or (lambda product: False)
        self.cache = OrderCache(directory / "orders-cache", ttl_s=6 * 3600)
        self.month_cache = OrderCache(directory / "months-cache", ttl_s=3600)
        self.previous = previous or {"rows": [], "memberships": []}
        self.orders, self.months, self.warnings = {}, {}, []
        self.order_slots = asyncio.Semaphore(3)
        self.month_slots = asyncio.Semaphore(2)
        self.cooldown = 0
        self.completed = 0
        self.total = 0

    async def get_json(self, url):
        async with self.order_slots:
            for attempt in range(3):
                await asyncio.sleep(max(0, self.cooldown - time.monotonic()))
                try:
                    response = await self.context.request.get(url, timeout=25_000)
                except Exception:
                    if attempt == 2:
                        raise ApiError("订单请求超时或网络连接失败") from None
                    await asyncio.sleep(2 ** attempt)
                    continue
                status = response.status
                try:
                    if status == 429 or status >= 500:
                        retry = response.headers.get("retry-after", "")
                        delay = min(30, max(2 ** (attempt + 1),
                                           int(retry) if retry.isdigit() else 0))
                        self.cooldown = max(self.cooldown, time.monotonic() + delay)
                        self.update(f"服务器暂时繁忙，降低请求速度，{delay} 秒后重试。")
                        self.log("request_retry", http_status=status, retry=attempt + 1,
                                 delay_seconds=delay)
                        continue
                    if status != 200:
                        raise ApiError(f"读取订单返回 HTTP {status}，请检查登录状态。")
                    return await response.json()
                finally:
                    await response.dispose()
            raise ApiError("服务器持续限流或暂时不可用，请稍后重试。")

    def checkpoint(self, complete=False):
        rows = []
        succeeded_urls = set()
        for gamekey in sorted(self.orders):
            order = self.orders[gamekey]
            for tpk in order_tpks(order):
                row = asdict(_extract_tpk(tpk, order))
                rows.append(row)
                succeeded_urls.add(row["humble_url"])
        # While scanning (or on partial failures), retain prior rows not yet refreshed.
        if not complete or self.warnings:
            rows += [r.copy() for r in self.previous.get("rows", [])
                     if r.get("humble_url") not in succeeded_urls]
        months = list(self.months.values())
        known = set(self.months)
        if not complete or self.warnings:
            months += [m for m in self.previous.get("memberships", [])
                       if m["url"] not in known
                       and "humble monthly" not in m.get("bundle_name", "").lower()]
        self.publish({"rows": rows, "memberships": months,
                      "warnings": self.warnings.copy(), "partial": not complete,
                      "scanned_at": datetime.now(timezone.utc).isoformat()})

    async def scan_order(self, gamekey):
        with scope(phase="读取订单", bundle="", game="", order_ref=order_ref(gamekey)):
            try:
                cached = None if self.full else self.cache.get(gamekey)
                tpks = order_tpks(cached) if cached else []
                # Refresh unresolved orders every run. Complete orders can reuse a six-hour cache.
                if cached and tpks and all(t.get("redeemed_key_val") for t in tpks):
                    order = cached
                else:
                    order = await self.get_json(ORDER_DETAIL_URL.format(gamekey=gamekey))
                    if not isinstance(order, dict):
                        raise ApiError("订单详情格式无法识别")
                    order.setdefault("gamekey", gamekey)
                    self.cache.put(gamekey, order)
                for tpk in order_tpks(order):
                    _extract_tpk(tpk, order)  # Validate before publishing or replacing old records.
                self.orders[gamekey] = order
            except Exception as exc:
                self.warnings.append(f"一个订单读取失败（{type(exc).__name__}）；旧记录保留。")
                self.log("order_failed", order_id=hashlib.sha256(gamekey.encode()).hexdigest()[:10],
                         **error_fields(exc))
            finally:
                self.completed += 1
                self.update(f"读取订单 {self.completed}/{self.total} · 最多 3 个请求并发")
                if self.completed % 20 == 0 or self.completed == self.total:
                    self.checkpoint()

    async def scan_month(self, order, url, index, total):
        with scope(phase="读取月包页面", game="",
                   bundle=(order.get("product") or {}).get("human_name", ""),
                   order_ref=order_ref(order.get("gamekey"))):
            async with self.month_slots:
                slug = url.rsplit("/", 1)[-1]
                cached = None if self.full else self.month_cache.get(slug)
                if cached and cached.get("state") == "complete":
                    self.months[url] = cached
                else:
                    page = await self.context.new_page()
                    cards = []
                    diagnostic = {"bundle": (order.get("product") or {}).get("human_name", ""),
                                  "month": slug, "reason": "no_cards"}
                    try:
                        response = await page.goto(
                            url, wait_until="domcontentloaded", timeout=30_000)
                        diagnostic["http_status"] = response.status if response else None
                        diagnostic["redirected"] = page.url.split("?")[0] != url
                        if "/login" in page.url:
                            diagnostic["reason"] = "redirected_to_login"
                            raise ApiError("登录会话失效")
                        if response and response.status >= 400:
                            diagnostic["reason"] = "http_error"
                            raise ApiError("月包页面请求失败")
                        await page.locator(SEL["card"]).first.wait_for(
                            state="attached", timeout=12_000)
                        cards = await page.locator(SEL["card"]).evaluate_all("""
                        elements => elements.map(e => ({
                            title: (e.querySelector('.content-choice-title, .content-title')
                                    ?.textContent || '').trim(),
                            claimed: e.classList.contains('claimed')
                        }))""")
                        if any(not c["title"] for c in cards):
                            diagnostic["reason"] = "card_title_selector_mismatch"
                            cards = []
                    except Exception as exc:
                        if diagnostic["reason"] == "no_cards":
                            diagnostic["reason"] = ("cards_not_found_or_load_timeout"
                                                    if "Timeout" in type(exc).__name__
                                                    else "navigation_failed")
                        diagnostic.update(error_fields(exc))
                        cards = []
                    finally:
                        await page.close()
                    month = self.describe(order, cards, url)
                    self.months[url] = month
                    if month["state"] == "unknown":
                        month["diagnostic"] = diagnostic
                        self.warnings.append(f"{month['bundle_name']}：月包页面待核查"
                                             f"（{diagnostic['reason']}），详见扫描日志。")
                        self.log("month_failed", **diagnostic)
                    else:
                        self.month_cache.put(slug, month)
                        self.log("month_scanned", month=slug, cards=len(cards),
                                 state=month["state"])
                self.update(f"检查月包 {len(self.months)}/{total} · 最多 2 个页面并行")
                if len(self.months) % 5 == 0 or len(self.months) == total:
                    self.checkpoint()

    async def run(self):
        body = await self.get_json(ORDERS_LIST_URL)
        if not isinstance(body, list):
            raise ApiError("订单列表格式无法识别")
        keys = list(dict.fromkeys(item if isinstance(item, str) else item.get("gamekey")
                                 for item in body if isinstance(item, (str, dict))))
        keys = [k for k in keys if isinstance(k, str) and k]
        if any(not re.fullmatch(r"[a-zA-Z0-9_-]+", k) for k in keys):
            raise ApiError("订单编号包含无法识别的字符")
        if body and not keys:
            raise ApiError("订单列表中未找到可识别的订单编号")
        self.total = len(keys)
        self.log("orders_listed", count=self.total, full_refresh=self.full)
        # Batch scheduling keeps very large libraries from allocating unlimited tasks.
        for start in range(0, len(keys), 30):
            await asyncio.gather(*(self.scan_order(k) for k in keys[start:start + 30]))
        self.checkpoint()
        targets = {}
        for order in self.orders.values():
            product = order.get("product") or {}
            if self.legacy(product):
                self.log("legacy_monthly_skipped", bundle=product.get("human_name", ""),
                         reason="旧版 Monthly 从订单读取 Key，不使用 Choice 选择页",
                         key_records=len(order_tpks(order)))
                continue
            url = self.target(product)
            if url:
                targets[url] = order
        items = list(targets.items())
        for start in range(0, len(items), 10):
            await asyncio.gather(*(self.scan_month(order, url, i, len(items))
                                   for i, (url, order) in enumerate(
                                       items[start:start + 10], start)))
        self.checkpoint(complete=True)
