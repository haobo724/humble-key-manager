"""Read-only inventory queries for the redesigned Humble storefront."""
import hashlib
import re
from dataclasses import asdict
from datetime import datetime, timezone

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from humble_bundle_keys.api import ApiError, ApiUnsupported, _normalise_platform, normalise_key
from humble_bundle_keys.models import GameKey
from humble_bundle_keys.site_session import (
    LIBRARY_URL,
    account_email,
    flight_records,
)


def is_library_page_response(response, page):
    request = response.request
    if response.url.partition("?")[0] != LIBRARY_URL or request.method != "POST":
        return False
    field = rf'name="_1_page"\r?\n\r?\n{page}\r?\n'
    return re.search(field, request.post_data or "") is not None


def query_result(text):
    results = [obj for obj in flight_records(text) if isinstance(obj, dict) and "results" in obj]
    if len(results) != 1 or results[0].get("success") is not True:
        raise ApiUnsupported("新版游戏库查询未返回有效结果；旧库存保留。")
    result = results[0]
    pagination = result.get("pagination")
    if not isinstance(result["results"], list) or not isinstance(pagination, dict):
        raise ApiUnsupported("新版游戏库分页格式变化；旧库存保留。")
    for name in ("page", "totalPages", "totalDocs"):
        if type(pagination.get(name)) is not int or pagination[name] < 0:
            raise ApiUnsupported("新版游戏库分页格式变化；旧库存保留。")
    return result


def rows_from_item(item):
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        raise ApiUnsupported("新版库存条目缺少标识。")
    identity = item["id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
        raise ApiUnsupported("新版库存标识无法识别。")
    entitlements = item.get("entitlements") or {}
    rows = []
    for entitlement in entitlements.get("keys", []):
        if not isinstance(entitlement, dict) or not isinstance(entitlement.get("id"), str):
            raise ApiUnsupported("新版 Key 记录格式变化。")
        redemption = entitlement.get("redemptionData") or {}
        key = normalise_key(redemption.get("keyFormatted"))
        title = item.get("title") or entitlement.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ApiUnsupported("新版库存缺少游戏名称。")
        deadline = entitlement.get("redemptionExpiryDate") or ""
        row = asdict(GameKey(
            game_title=title, platform=_normalise_platform(entitlement.get("type")) or "unknown",
            key=key,
            bundle_name=item.get("subTitle") or entitlement.get("subTitle") or "",
            humble_url=f"{LIBRARY_URL}/content/{identity}",
            redemption_deadline=f"Must be redeemed by {deadline}" if deadline else "",
            redeemed_on_humble=bool(key or entitlement.get("redeemedAt")),
        ))
        row.update(modern_item_id=identity, modern_entitlement_id=entitlement["id"],
                   modern_content_id=item.get("contentId"),
                   library_url=row["humble_url"], source="humble_next",
                   upstream_expired=bool(entitlement.get("expired")),
                   upstream_out_of_stock=bool(entitlement.get("outOfStock")),
                   gift_status=entitlement.get("giftStatus"))
        if isinstance(entitlement.get("redeemedAt"), str) and re.match(
                r"^\d{4}-\d{2}-\d{2}T", entitlement["redeemedAt"]):
            row["revealed_at"] = entitlement["redeemedAt"]
        if entitlement.get("outOfStock") and not key:
            row.update(unavailable_status="key_temporarily_exhausted",
                       unavailable_reason="Key 暂时缺货，补货后可重试")
        if entitlement.get("expired") and not key:
            row.update(unavailable_status="expired", unavailable_reason="已过期，无法领取或兑换")
            if not deadline:
                row["redemption_deadline"] = "Expired"
        rows.append(row)
    return rows


def merge_rows(previous, fresh):
    """Retain historical records during migration, including their selection identities."""
    rows = [dict(row) for row in previous]
    for new in fresh:
        exact = [i for i, old in enumerate(rows) if old.get("modern_entitlement_id")
                 == new["modern_entitlement_id"]]
        if not exact and new.get("key"):
            exact = [i for i, old in enumerate(rows) if old.get("key") == new["key"]
                     and old.get("platform") == new["platform"]]
        if not exact:
            exact = [i for i, old in enumerate(rows) if all(
                old.get(k) == new.get(k) for k in ("game_title", "bundle_name", "platform"))
                and (not old.get("key") or not new.get("key") and new.get("revealed_at"))]
        if len(exact) != 1:
            rows.append(new)
            continue
        index = exact[0]
        old = rows[index]
        merged = {**old, **new, "humble_url": old.get("humble_url") or new["humble_url"]}
        for name in ("key", "revealed_at", "steam_app_id", "exclusive_countries",
                     "disallowed_countries", "redemption_deadline", "os_support"):
            if new.get(name) in (None, "") and old.get(name) not in (None, ""):
                merged[name] = old[name]
        if merged.get("key"):
            merged.pop("unavailable_status", None)
            merged.pop("unavailable_reason", None)
        elif ((merged.get("unavailable_status") == "key_temporarily_exhausted"
               and not new.get("upstream_out_of_stock")) or
              (merged.get("unavailable_status") == "expired" and not new.get("upstream_expired"))):
            merged.pop("unavailable_status", None)
            merged.pop("unavailable_reason", None)
        rows[index] = merged
    return rows


class NewLibraryScanner:
    def __init__(self, context, publish, update, previous, account, log=None):
        self.context, self.publish, self.update = context, publish, update
        self.previous, self.account = previous, account
        self.log = log or (lambda *args, **kwargs: None)
        self.page = None

    async def open_library(self):
        self.page = await self.context.new_page()
        async with self.page.expect_response(
            lambda response: is_library_page_response(response, 1), timeout=45_000
        ) as first:
            document = await self.page.goto(
                LIBRARY_URL, wait_until="domcontentloaded", timeout=30_000)
            if document is None or document.status != 200:
                status = document.status if document else "无响应"
                raise ApiError(f"新版官网读取失败（HTTP {status}）；旧库存保留。")
            identity = hashlib.sha256(account_email(await document.text()).encode()).hexdigest()
            if identity != self.account:
                raise ApiError("新版库账号与当前库存不一致；未更新库存，请重新登录。")
        return await self.read_page(await first.value, 1)

    async def read_page(self, response, page):
        if response.status != 200:
            raise ApiError(
                f"官网游戏库第 {page} 页查询失败（HTTP {response.status}）；旧库存保留。")
        result = query_result(await response.text())
        if result["pagination"]["page"] != page:
            raise ApiUnsupported("新版库返回了错误页码；旧库存保留。")
        return result

    async def query(self, page):
        nav = self.page.locator('nav[aria-label="pagination"]')
        await nav.locator(f'[aria-current="page"][href="/home/library?page={page - 1}"]').wait_for(
            timeout=20_000)
        next_link = nav.locator(f'a[href="/home/library?page={page}"]').filter(
            has_text=re.compile(r"^\s*Next\s*$"))
        async with self.page.expect_response(
            lambda response: is_library_page_response(response, page), timeout=30_000
        ) as pending:
            await next_link.click(timeout=15_000)
        return await self.read_page(await pending.value, page)

    async def run(self):
        try:
            await self.scan_pages()
        except PlaywrightTimeoutError as exc:
            raise ApiError("等待官网游戏库加载或翻页超时；旧库存保留。") from exc
        finally:
            if self.page is not None:
                await self.page.close()

    async def scan_pages(self):
        first = await self.open_library()
        total = first["pagination"]["totalPages"]
        if total > 2000:
            raise ApiUnsupported("官网返回的分页数量异常；旧库存保留。")
        items = list(first["results"])
        self.update(f"新版游戏库：已读取 1 / {max(1, total)} 页。")
        for number in range(2, total + 1):
            page = await self.query(number)
            pagination = page["pagination"]
            if (pagination["totalDocs"] != first["pagination"]["totalDocs"]
                    or pagination["totalPages"] != total):
                raise ApiError("扫描期间官网库存发生变化，请重新扫描；旧库存保留。")
            items.extend(page["results"])
            self.update(f"新版游戏库：已读取 {number} / {total} 页。")
        if len(items) != first["pagination"]["totalDocs"]:
            raise ApiUnsupported("新版库记录数与分页汇总不一致；旧库存保留。")
        unique = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ApiUnsupported("新版库条目格式变化；旧库存保留。")
            identity = item["id"]
            if identity in unique and unique[identity] != item:
                raise ApiUnsupported("官网分页中同一条目内容冲突；旧库存保留，请稍后重试。")
            unique[identity] = item
        duplicate_count = len(items) - len(unique)
        fresh = [row for item in unique.values() for row in rows_from_item(item)]
        warnings = ["Humble 官网正在迁移历史订单：新版扫描会保留旧库存，不自动删除缺失记录。"]
        groups = [item for item in unique.values() if item.get("itemType") == "entitlement-group"]
        memberships = list(self.previous.get("memberships", []))
        known = {month["url"] for month in memberships}
        for group in groups:
            url = f"{LIBRARY_URL}/group/{group['id']}"
            if url not in known:
                memberships.append({"url": url, "bundle_name": group.get("title") or "官网分组",
                                    "choice_policy": "unknown", "state": "unknown",
                                    "unclaimed_titles": [], "total_games": 0, "claimed_games": 0,
                                    "source": "humble_next"})
        if groups:
            warnings.append(f"发现 {len(groups)} 个新版分组，可在「月包待选择 / 领取」"
                            "中打开并选择游戏、平台；扫描本身不领取。")
        if duplicate_count:
            warnings.append(f"官网分页含 {duplicate_count} 条完全重复记录，已去重；"
                            "保留所有旧库存，可稍后重扫核对迁移变动。")
        self.publish({"rows": merge_rows(self.previous.get("rows", []), fresh),
                      "memberships": memberships, "warnings": warnings,
                      "partial": True, "backend": "humble_next", "upstream_total": len(items),
                      "scanned_at": datetime.now(timezone.utc).isoformat()})
        self.log("new_library_scan_complete", rows=len(fresh), count=len(items),
                 source="humble_next", reason="legacy_inventory_retained_during_migration")
