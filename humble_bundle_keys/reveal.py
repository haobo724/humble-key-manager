"""Sequential key revelation. Limited/unknown Choice months are never selected."""
import logging
import time
from dataclasses import asdict

from humble_bundle_keys._orders_cache import OrderCache
from humble_bundle_keys.api import ApiOptions, ApiScraper, _extract_tpk
from humble_bundle_keys.async_inventory import order_tpks
from humble_bundle_keys.browser_choice import (
    SEL,
    BrowserChoiceClaimer,
    BrowserClaimAttempt,
    BrowserClaimOptions,
    derive_membership_slug,
)
from humble_bundle_keys.choice import categorize_keytype
from humble_bundle_keys.choice_policy import choice_policy
from humble_bundle_keys.deadlines import deadline_info
from humble_bundle_keys.operation_report import work_id
from humble_bundle_keys.steam import row_id

KEY_PLATFORMS = {"steam", "gog", "origin", "uplay", "rockstar", "battlenet", "xbox", "microsoft"}


def eligible_tpk(order, tpk):
    game = _extract_tpk(tpk, order)
    if game.key or deadline_info(game.redemption_deadline)["deadline_state"] == "expired":
        return False
    if game.platform not in KEY_PLATFORMS:
        return False
    if categorize_keytype(tpk.get("machine_name")) in {
        "voucher", "keyless", "softwarebundle", "freegame",
    }:
        return False
    product = order.get("product") or {}
    if product.get("category") == "subscriptioncontent" and not (
        product.get("machine_name", "").endswith("_monthly")
    ):
        return choice_policy(product) == "all_games"
    if categorize_keytype(tpk.get("machine_name")) == "choice":
        return choice_policy(product) == "all_games"
    return True


def preview_reveal(snapshot):
    months = snapshot.get("memberships", [])
    modern_names = {m["bundle_name"] for m in months if m.get("choice_policy") == "all_games"}
    limited_names = {m["bundle_name"] for m in months if m.get("choice_policy") != "all_games"}
    rows = [r for r in snapshot.get("rows", []) if not r.get("key")
            and r.get("platform") in KEY_PLATFORMS and r.get("deadline_state") != "expired"
            and r.get("bundle_name") not in limited_names]
    return {"key_records": len(rows), "modern_months": sum(
        m.get("choice_policy") == "all_games" and m["state"] == "pending" for m in months),
        "modern_games": sum(len(m["unclaimed_titles"]) for m in months
                            if m.get("choice_policy") == "all_games" and m["state"] == "pending"),
        "skipped_limited_months": len(limited_names), "revision": snapshot.get("revision", 0),
        "ordinary_records": sum(r["bundle_name"] not in modern_names for r in rows)}


def reveal_all(context, inventory, selected_ids=None):
    inventory.begin_report("reveal-selected" if selected_ids is not None else "reveal",
                           estimated=True)
    inventory.report_phase("核对订单与可刮取项目")
    selected = None if selected_ids is None else {
        (r.get("humble_url"), r.get("game_title"), r.get("platform"))
        for r in inventory.snapshot["rows"] if row_id(r) in set(selected_ids)}
    scraper = ApiScraper(context, ApiOptions(reveal_keys=False, dry_run=True))
    cache = OrderCache(inventory.directory / "orders-cache")
    succeeded = failed = 0
    inventory.update("正在重新核对账户订单和可刮取项目…")
    games, stats = scraper.scrape()  # revalidate using the current session, never cached plans
    base = {"rows": [asdict(g) for g in games], "memberships": inventory.snapshot["memberships"],
            "warnings": [], "scanned_at": inventory.snapshot.get("scanned_at"), "partial": True}
    if stats.errors:
        fresh = {(r["humble_url"], r["game_title"], r["platform"]) for r in base["rows"]}
        base["rows"] += [r.copy() for r in inventory.snapshot["rows"]
                         if (r["humble_url"], r["game_title"], r["platform"]) not in fresh]
        base["warnings"].append("部分订单未读取成功，旧记录保留；失败订单未执行刮取。")
    inventory.publish(base)
    if stats.errors:
        inventory.report_issue(f"{len(stats.errors)} 个订单读取失败，未执行刮取。")
    # Logical game items are shared by the API attempt and its browser fallback.
    # This prevents counting one Choice game twice when the API returns no key.
    for order in scraper.orders:
        bundle = (order.get("product") or {}).get("human_name", "")
        for tpk in order_tpks(order):
            game = _extract_tpk(tpk, order)
            if selected is not None and (
                game.humble_url, game.game_title, game.platform
            ) not in selected:
                continue
            if selected is None and game.key:
                continue  # Global revelation only plans unseen keys.
            identity = work_id(order["gamekey"], game.game_title, game.platform)
            reason = "已显示 Key" if game.key else "已过期" if deadline_info(
                game.redemption_deadline)["deadline_state"] == "expired" else "规则或交付方式不支持"
            inventory.report_item(identity, game.game_title, bundle,
                                  status=None if eligible_tpk(order, tpk) else "skipped",
                                  reason="" if eligible_tpk(order, tpk) else reason)
        if choice_policy(order.get("product") or {}) != "all_games":
            continue
        for month in inventory.snapshot["memberships"]:
            if month["bundle_name"] != bundle:
                continue
            for title in month["unclaimed_titles"]:
                if selected is not None and not any(
                    r[0] == f"https://www.humblebundle.com/downloads?key={order['gamekey']}"
                    and r[1] == title for r in selected
                ):
                    continue
                inventory.report_item(work_id(order["gamekey"], title, "steam"), title, bundle)

    def save_key(order, tpk, key):
        tpk["redeemed_key_val"] = key
        game = _extract_tpk(tpk, order)
        for i, row in enumerate(base["rows"]):
            if (not row.get("key") or row["key"] == key) and (
                row["humble_url"], row["game_title"], row["platform"]
            ) == (
                game.humble_url, game.game_title, game.platform,
            ):
                base["rows"][i] = asdict(game)
                break
        else:
            base["rows"].append(asdict(game))
        inventory.publish(base)
        cache.invalidate(order["gamekey"])

    try:
        for order in scraper.orders:
            if inventory.stop_event.is_set():
                return
            product = order.get("product") or {}
            modern = choice_policy(product) == "all_games"
            for tpk in order_tpks(order):
                if inventory.stop_event.is_set():
                    return
                if not eligible_tpk(order, tpk):
                    continue
                game = _extract_tpk(tpk, order)
                if selected is not None and (
                    game.humble_url, game.game_title, game.platform
                ) not in selected:
                    continue
                inventory.update(f"正在刮取：{tpk.get('human_name', '游戏')} · 已成功 {succeeded}")
                identity = work_id(order["gamekey"], game.game_title, game.platform)
                bundle = product.get("human_name", "")
                inventory.report_phase("刮取 Key", game.game_title, bundle)
                try:
                    key = scraper._reveal(tpk, order)
                    if key:
                        save_key(order, tpk, key)
                        succeeded += 1
                        inventory.report_item(identity, game.game_title, bundle, "success")
                        inventory.log("key_revealed", game=tpk.get("human_name", ""))
                    elif not modern:
                        failed += 1
                        inventory.report_item(identity, game.game_title, bundle, "failed",
                                              "未返回 Key")
                        inventory.log("reveal_no_key", game=tpk.get("human_name", ""))
                except Exception as exc:
                    failed += 1
                    inventory.report_item(identity, game.game_title, bundle, "failed",
                                          type(exc).__name__)
                    inventory.log("reveal_failed", game=tpk.get("human_name", ""),
                                  error_type=type(exc).__name__)
                    if "429" in str(exc) or "403" in str(exc):
                        inventory.update("请求受限，已停止刮取；成功的 Key 已保存。")
                        inventory.report_issue("请求受限，提前停止；未处理项目保留。")
                        return
                time.sleep(2)
            if not modern:
                continue
            if selected is not None and not any(
                r.get("bundle_name") == product.get("human_name") and row_id(r) in selected_ids
                for r in inventory.snapshot["rows"]
            ):
                continue
            # Modern months have no selection budget. Legacy/unknown months never reach here.
            slug = derive_membership_slug(product)
            if not slug or not re_safe_slug(slug):
                continue
            page = context.new_page()
            claimer = BrowserChoiceClaimer(context, BrowserClaimOptions(polite_delay_s=3))
            logger = logging.getLogger("humble_bundle_keys.browser_choice")
            previous_disabled = logger.disabled
            logger.disabled = True  # upstream INFO logs may include raw keys
            try:
                page.goto(f"https://www.humblebundle.com/membership/{slug}",
                          wait_until="domcontentloaded", timeout=30_000)
                page.locator(SEL["card"]).first.wait_for(state="attached", timeout=12_000)
                count = page.locator(SEL["card"]).count()
                for index in range(count):
                    if inventory.stop_event.is_set():
                        return
                    card = page.locator(SEL["card"]).nth(index)
                    title = claimer._read_title(card)
                    identity = work_id(order["gamekey"], title, "steam")
                    if "claimed" in (card.get_attribute("class") or "").split():
                        if inventory.report_has(identity):
                            inventory.report_item(identity, title, product.get("human_name", ""),
                                                  "skipped", "已领取")
                        continue
                    if selected is not None and not any(
                        identity[1] == title and identity[0] ==
                        f"https://www.humblebundle.com/downloads?key={order.get('gamekey')}"
                        for identity in selected
                    ):
                        continue
                    inventory.update(f"正在领取并刮取：{title} · 已成功 {succeeded}")
                    inventory.report_item(identity, title, product.get("human_name", ""))
                    inventory.report_phase("领取并刮取", title, product.get("human_name", ""))
                    attempt = BrowserClaimAttempt(slug=slug, title=title)
                    claimer._claim_single_card(page, index, attempt)
                    if attempt.key:
                        matching = next((t for t in order_tpks(order)
                                         if t.get("human_name") == title), None)
                        if matching is None:
                            matching = {"human_name": title, "key_type": "steam"}
                        save_key(order, matching, attempt.key)
                        succeeded += 1
                        inventory.report_item(identity, title, product.get("human_name", ""),
                                              "success")
                        inventory.log("choice_key_revealed", month=slug, game=title)
                    else:
                        if attempt.key_exhausted:
                            inventory.report_item(identity, title, product.get("human_name", ""),
                                                  "skipped", "Key 暂时缺货；补货后可再次刮取")
                            inventory.log("choice_reveal_skipped", month=slug, game=title,
                                          reason="key_temporarily_exhausted")
                            continue
                        if attempt.error == "epic account linking required":
                            inventory.report_item(identity, title, product.get("human_name", ""),
                                                  "skipped", "需关联 Epic 账号领取")
                            inventory.log("choice_reveal_skipped", month=slug, game=title,
                                          reason="epic_account_link_required")
                            continue
                        failed += 1
                        expired = attempt.error == "key expired upstream"
                        inventory.report_item(identity, title, product.get("human_name", ""),
                                              "skipped" if expired else "failed",
                                              "已过期" if expired else "未取得 Key 或不可领取")
                        inventory.log("choice_reveal_failed", month=slug, game=title,
                                      reason="expired_or_unavailable" if attempt.already_claimed
                                      else "no_key_returned")
                    time.sleep(3)
            except Exception as exc:
                failed += 1
                inventory.log("choice_reveal_page_failed", month=slug,
                              error_type=type(exc).__name__)
                inventory.report_issue(f"{product.get('human_name', slug)} 页面处理失败"
                                       f"（{type(exc).__name__}），有项目未完成。")
            finally:
                logger.disabled = previous_disabled
                page.close()
    finally:
        scraper.close_anchor_page()
        inventory.log("reveal_complete", succeeded=succeeded, failed=failed,
                      skipped_orders=len(stats.errors))
    inventory.update(f"刮取结束：成功 {succeeded}，失败或不可领取 {failed}。正在更新列表…")
    inventory.report_phase("刷新列表，Key 已保存")


def re_safe_slug(slug):
    return all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in slug)
