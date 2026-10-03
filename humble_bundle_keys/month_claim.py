"""Explicit per-month Choice plans. Previews only read; confirmed claims consume slots."""
import copy
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

from playwright.sync_api import sync_playwright

from humble_bundle_keys._orders_cache import OrderCache
from humble_bundle_keys.api import ORDER_DETAIL_URL, ORDERS_LIST_URL, _extract_tpk
from humble_bundle_keys.async_inventory import order_tpks
from humble_bundle_keys.browser_choice import (
    SEL,
    BrowserChoiceClaimer,
    BrowserClaimAttempt,
    BrowserClaimOptions,
    derive_membership_slug,
)
from humble_bundle_keys.choice_policy import choice_policy
from humble_bundle_keys.operation_report import work_id


def month_slug(url):
    match = re.fullmatch(r"https://www\.humblebundle\.com/membership/([a-z]+-\d{4})", url)
    if not match:
        raise ValueError("月包地址无法识别，请重新扫描。")
    return match[1]


def remaining_choices(order):
    value = order.get("choices_remaining")
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return value if type(value) is int and value >= 0 else None


def validate_selection(policy, remaining, titles, unclaimed):
    if policy not in {"limited", "all_games"}:
        raise ValueError("选择规则未知，请到原页面核查。")
    if not titles or len(titles) != len(set(titles)):
        raise ValueError("请选择游戏，且不要重复选择。")
    if any(unclaimed.count(title) != 1 for title in titles):
        raise ValueError("选中的游戏已领取、不存在或存在同名卡片，请重新预览。")
    if policy == "limited":
        if remaining is None:
            raise ValueError("无法确认剩余额度，请重新扫描或在原页面核查。")
        if len(titles) > remaining:
            raise ValueError(f"选择了 {len(titles)} 个游戏，但剩余额度只有 {remaining}。")


def get_json(context, url):
    response = context.request.get(url, timeout=25_000)
    try:
        if response.status != 200:
            raise RuntimeError(f"读取 Humble 返回 HTTP {response.status}，未执行领取。")
        return response.json()
    finally:
        response.dispose()


def order_slug(order):
    slug = derive_membership_slug(order.get("product") or {})
    if slug and slug.startswith("https://www.humblebundle.com/membership/"):
        slug = slug.rsplit("/", 1)[-1]
    return slug


def find_orders(context, directory, urls):
    """Check current-account order IDs before using any cached order-to-month mapping."""
    wanted = {month_slug(url) for url in urls}
    body = get_json(context, ORDERS_LIST_URL)
    if not isinstance(body, list):
        raise RuntimeError("订单列表格式无法识别，未执行领取。")
    keys = {item if isinstance(item, str) else item.get("gamekey")
            for item in body if isinstance(item, (str, dict))}
    keys = {key for key in keys if isinstance(key, str) and re.fullmatch(r"[\w-]+", key)}
    cache = OrderCache(directory / "orders-cache", ttl_s=10**10)
    result = {}
    uncached = []
    for key in sorted(keys):
        cached = cache.get(key)
        if not cached:
            uncached.append(key)
            continue
        slug = order_slug(cached)
        if slug in wanted:
            if slug in result:
                raise RuntimeError("同一月份对应多个订单，请到原页面核查。")
            order = get_json(context, ORDER_DETAIL_URL.format(gamekey=key))
            if not isinstance(order, dict) or order_slug(order) != slug:
                raise RuntimeError("订单与月包不一致，请重新扫描。")
            order["gamekey"] = key
            result[slug] = order
    if wanted - result.keys():
        for key in uncached:
            order = get_json(context, ORDER_DETAIL_URL.format(gamekey=key))
            if not isinstance(order, dict):
                continue
            order["gamekey"] = key
            cache.put(key, order)
            slug = order_slug(order)
            if slug in wanted:
                if slug in result:
                    raise RuntimeError("同一月份对应多个订单，请到原页面核查。")
                result[slug] = order
    if wanted - result.keys():
        raise ValueError("当前账户中未找到选中的月包，请确认账户并重新扫描。")
    return result


def read_cards(page, url):
    response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    if page.url.split("?")[0].rstrip("/") != url or (response and response.status >= 400):
        raise RuntimeError("月包页面未正常打开，未执行领取。")
    page.locator(SEL["card"]).first.wait_for(state="attached", timeout=12_000)
    cards = page.locator(SEL["card"]).evaluate_all("""elements => elements.map(e => ({
        title:(e.querySelector('.content-choice-title, .content-title')?.textContent||'').trim(),
        claimed:e.classList.contains('claimed')
    }))""")
    if not cards or any(not card["title"] for card in cards):
        raise ValueError("月包游戏卡片无法识别，未执行领取。")
    return cards


def preview_months(context, inventory, requests):
    """Return a frozen list of approved game titles after checking fresh quotas and DOM."""
    orders = find_orders(context, inventory.directory, [r["url"] for r in requests])
    targets = []
    snapshot = copy.deepcopy(inventory.snapshot)
    page = context.new_page()
    try:
        for request in requests:
            if inventory.stop_event.is_set():
                raise ValueError("预览已停止，未执行领取。")
            url = request["url"]
            order = orders[month_slug(url)]
            policy = choice_policy(order.get("product") or {}, url)
            inventory.update(f"正在核对月包额度与游戏：{order['product'].get('human_name', '')}")
            cards = read_cards(page, url)
            unclaimed = [c["title"] for c in cards if not c["claimed"]]
            if request.get("all_games") is True:
                if policy != "all_games":
                    raise ValueError("仅无限额 Choice 可以领取本月全部游戏。")
                titles = unclaimed
            else:
                titles = request["titles"]
            remaining = remaining_choices(order) if policy == "limited" else None
            month = next((m for m in snapshot["memberships"] if m["url"] == url), None)
            if month:
                month.update({"choices_remaining": remaining, "unclaimed_titles": unclaimed,
                              "claimed_games": sum(c["claimed"] for c in cards),
                              "total_games": len(cards), "state": "complete" if not unclaimed
                              else "exhausted" if policy == "limited" and remaining == 0
                              else "pending"})
            if not titles and request.get("all_games") is True:
                continue
            try:
                validate_selection(policy, remaining, titles, unclaimed)
            except ValueError:
                inventory.publish(snapshot)  # Show the actual quota even when a plan is rejected.
                raise
            target = {"url": url, "titles": titles, "choice_policy": policy,
                      "choices_remaining": remaining,
                      "bundle_name": order["product"].get("human_name", "月包")}
            targets.append(target)
    finally:
        page.close()
    inventory.publish(snapshot)
    if not targets:
        raise ValueError("所选月份已经全部领取，没有需要刮取的游戏。")
    return targets


def refresh_month(context, inventory, order, url, page):
    """Persist actual quota, cards and any keys after every attempt, including partial failure."""
    fresh = get_json(context, ORDER_DETAIL_URL.format(gamekey=order["gamekey"]))
    if not isinstance(fresh, dict) or order_slug(fresh) != month_slug(url):
        raise RuntimeError("领取后的订单无法核查，请在原页面确认。")
    fresh["gamekey"] = order["gamekey"]
    cards = read_cards(page, url)
    with inventory.lock:
        snapshot = copy.deepcopy(inventory.snapshot)
        from humble_bundle_keys.web import describe_membership

        month = describe_membership(fresh, cards, url)
        snapshot["memberships"] = [month if m["url"] == url else m
                                   for m in snapshot["memberships"]]
        new_rows = [asdict(_extract_tpk(tpk, fresh)) for tpk in order_tpks(fresh)]
        # Preserve keys extracted from a modal when the order API has not caught up yet.
        for row in new_rows:
            if row["key"]:
                continue
            existing = next((r for r in snapshot["rows"] if r.get("key") and
                             r["game_title"] == row["game_title"] and
                             r["humble_url"] == row["humble_url"] and
                             r["platform"] in {row["platform"], "unknown"}), None)
            if existing:
                row["key"] = existing["key"]
                row["redeemed_on_humble"] = True
        urls = {r["humble_url"] for r in new_rows}
        if new_rows:
            missing_keys = [r for r in snapshot["rows"] if r.get("key") and
                            r["humble_url"] in urls and not any(
                                n["game_title"] == r["game_title"] and
                                (n["platform"] == r["platform"] or r["platform"] == "unknown")
                                for n in new_rows)]
            snapshot["rows"] = [r for r in snapshot["rows"] if r["humble_url"] not in urls]
            snapshot["rows"] += new_rows + missing_keys
        inventory.publish(snapshot)
    OrderCache(inventory.directory / "orders-cache").put(fresh["gamekey"], fresh)
    OrderCache(inventory.directory / "months-cache").invalidate(month_slug(url))
    return fresh, cards


def save_modal_key(inventory, order, title, key):
    with inventory.lock:
        snapshot = copy.deepcopy(inventory.snapshot)
        tpk = next((t for t in order_tpks(order) if t.get("human_name") == title),
                   {"human_name": title, "key_type": "unknown"})
        row = asdict(_extract_tpk({**tpk, "redeemed_key_val": key}, order))
        existing = next((i for i, r in enumerate(snapshot["rows"])
                         if r["game_title"] == title and r["humble_url"] == row["humble_url"]
                         and (not r.get("key") or r["key"] == key)), None)
        if existing is None:
            snapshot["rows"].append(row)
        else:
            # Preserve already-known delivery metadata while the order updates asynchronously.
            if row["platform"] == "unknown":
                row["platform"] = snapshot["rows"][existing].get("platform", "unknown")
                row["steam_app_id"] = snapshot["rows"][existing].get("steam_app_id")
            snapshot["rows"][existing] = row
        inventory.publish(snapshot)


def _claim_serial(context, inventory, targets):
    """Only the frozen, confirmed titles may consume quota; never automatically retry a failure."""
    page = context.new_page()
    claimer = BrowserChoiceClaimer(context, BrowserClaimOptions(polite_delay_s=3))
    succeeded = 0
    try:
        for target in targets:
            url, titles = target["url"], target["titles"]
            if inventory.stop_event.is_set():
                inventory.update("月包领取已停止；已取得的 Key 和额度进度已保存。")
                return
            inventory.report_phase("核对当前月份", bundle=target["bundle_name"])
            orders = find_orders(context, inventory.directory, [url])
            order = orders[month_slug(url)]
            for position, title in enumerate(titles):
                if inventory.stop_event.is_set():
                    inventory.update("月包领取已停止；已取得的 Key 和额度进度已保存。")
                    return
                order, cards = refresh_month(context, inventory, order, url, page)
                policy = choice_policy(order.get("product") or {}, url)
                if (policy != target["choice_policy"] or
                        (position == 0 and policy == "limited" and
                         remaining_choices(order) != target["choices_remaining"])):
                    raise ValueError("月包规则或剩余额度已经变化，请重新预览确认。")
                available = [c["title"] for c in cards if not c["claimed"]]
                validate_selection(policy, remaining_choices(order), titles[position:], available)
                index = next(i for i, card in enumerate(cards) if card["title"] == title)
                inventory.update(f"领取并刮取：{target['bundle_name']} · {title}")
                identity = work_id(url, title)
                inventory.report_phase("领取并刮取", title, target["bundle_name"])
                attempt = BrowserClaimAttempt(slug=month_slug(url), title=title)
                failure_type = ""
                try:
                    claimer._claim_single_card(page, index, attempt)
                except Exception as exc:
                    failure_type = type(exc).__name__
                    inventory.log("month_claim_error", month=month_slug(url), game=title,
                                  error_type=type(exc).__name__)
                if attempt.error == "epic account linking required":
                    inventory.report_item(identity, title, target["bundle_name"], "skipped",
                                          "需关联 Epic 账号领取，不提供可刮取的 Steam Key")
                    inventory.log("month_claim_skipped", month=month_slug(url), game=title,
                                  reason="epic_account_link_required")
                    continue
                if attempt.key:
                    save_modal_key(inventory, order, title, attempt.key)
                    succeeded += 1
                    inventory.report_item(identity, title, target["bundle_name"], "success")
                    inventory.log("month_key_revealed", month=month_slug(url), game=title)
                # A timeout may have consumed a slot: refresh and stop, never continue blind.
                order, fresh_cards = refresh_month(context, inventory, order, url, page)
                if not attempt.key:
                    # Recover an already returned key without submitting another claim.
                    recovered = next((t.get("redeemed_key_val") for t in order_tpks(order)
                                      if t.get("human_name") == title and
                                      t.get("redeemed_key_val")), "")
                    if recovered:
                        attempt.key = recovered
                        save_modal_key(inventory, order, title, recovered)
                        succeeded += 1
                        inventory.report_item(identity, title, target["bundle_name"], "success")
                        inventory.log("month_key_recovered", month=month_slug(url), game=title,
                                      source="read_only_order_refresh")
                if attempt.key_exhausted and not attempt.key:
                    inventory.report_item(identity, title, target["bundle_name"], "skipped",
                                          "Key 暂时缺货；补货后可再次刮取")
                    inventory.log("month_claim_skipped", month=month_slug(url), game=title,
                                  reason="key_temporarily_exhausted")
                    continue
                if attempt.error == "key expired upstream":
                    inventory.log("month_claim_skipped", month=month_slug(url), game=title,
                                  reason="expired")
                    inventory.report_item(identity, title, target["bundle_name"], "skipped",
                                          "已过期")
                    continue
                if not attempt.key:
                    claimed = any(c["title"] == title and c["claimed"] for c in fresh_cards)
                    reason = ("Humble 已标记领取，但尚未取得 Key；请在已刮取/未刮取列表或原页面核查"
                              if claimed else "未取得 Key，领取结果需核查")
                    if attempt.key_exhausted:
                        reason = "Humble 提示 Key 暂时缺货；本次未取得 Key"
                    elif attempt.error.startswith("key field never populated"):
                        reason += "（等待 Key 超时）"
                    elif failure_type:
                        reason += f"（{failure_type}）"
                    elif attempt.error:
                        # Only emit fixed categories: upstream errors may contain URLs or keys.
                        category = next((label for prefix, label in (
                            ("card click failed", "卡片打开失败"),
                            ("modal didn't open", "弹窗未打开"),
                            ("'Get Game on Steam' button not found", "刮取按钮未找到"),
                            ("Get-Game-on-Steam click failed", "刮取按钮点击失败"),
                            ("already claimed", "页面提示已领取"),
                        ) if attempt.error.startswith(prefix)), "页面未返回 Key")
                        reason += f"（{category}）"
                    inventory.log("month_claim_stopped", month=month_slug(url), game=title,
                                  reason=reason, claimed=claimed,
                                  key_exhausted=attempt.key_exhausted)
                    inventory.update(reason + "；已停止并更新实际额度，其余项目未处理。")
                    inventory.report_item(identity, title, target["bundle_name"], "failed",
                                          reason)
                    return
                if inventory.stop_event.wait(3):
                    inventory.update("月包领取已停止；已取得的 Key 和额度进度已保存。")
                    return
        inventory.update(f"月包领取完成：取得 {succeeded} 枚 Key；额度与列表已更新。")
        inventory.log("month_claim_complete", succeeded=succeeded)
        return True
    finally:
        page.close()


def _claim_worker(storage_state, inventory, target):
    # Playwright sync objects belong to their thread. Never share the parent context.
    from humble_bundle_keys.web import available_browser_channel
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, channel=available_browser_channel())
        try:
            context = browser.new_context(storage_state=storage_state)
            return _claim_serial(context, inventory, [target])
        finally:
            browser.close()


def claim_months(context, inventory, targets):
    """Two unlimited months at once; each month and all limited selections stay serial."""
    if len({target["url"] for target in targets}) != len(targets):
        raise ValueError("同一月份不能重复执行。")
    inventory.begin_report("claim-months")
    inventory.report_phase("准备逐月核对并刮取")
    for target in targets:
        for title in target["titles"]:
            inventory.report_item(work_id(target["url"], title), title, target["bundle_name"])
    unlimited = [t for t in targets if t["choice_policy"] == "all_games"]
    limited = [t for t in targets if t["choice_policy"] != "all_games"]
    logger = logging.getLogger("humble_bundle_keys.browser_choice")
    previous = logger.disabled
    logger.disabled = True  # upstream logs may contain keys
    try:
        if len(unlimited) < 2:
            return _claim_serial(context, inventory, targets)
        state = context.storage_state()
        inventory.log("month_parallel_started", workers=2, months=len(unlimited))
        # Only two pending jobs: do not launch queued months after an uncertain result.
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = {}
            queue = iter(unlimited)
            for _ in range(2):
                target = next(queue, None)
                if target:
                    pending[pool.submit(_claim_worker, state, inventory, target)] = target
            while pending:
                future = next(as_completed(pending))
                target = pending.pop(future)
                try:
                    completed = future.result()
                except Exception as exc:
                    inventory.stop_event.set()
                    inventory.report_issue(f"{target['bundle_name']} 处理异常"
                                           f"（{type(exc).__name__}）")
                    raise
                if not completed:
                    inventory.stop_event.set()
                if not inventory.stop_event.is_set():
                    target = next(queue, None)
                    if target:
                        pending[pool.submit(_claim_worker, state, inventory, target)] = target
        if inventory.stop_event.is_set():
            inventory.update("并行刮取已停止；已取得的 Key 已保存，未处理项目保留。")
            return
        if limited and not _claim_serial(context, inventory, limited):
            return
        report = inventory.report.view()
        inventory.update(f"月包刮取完成：成功 {report['succeeded']}，跳过 {report['skipped']}，"
                         f"失败 {report['failed']}。")
    finally:
        logger.disabled = previous
