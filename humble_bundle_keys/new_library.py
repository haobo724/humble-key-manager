"""Read-only inventory queries for the redesigned Humble storefront."""
import asyncio
import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone

from humble_bundle_keys.api import ApiError, ApiUnsupported, _normalise_platform, normalise_key
from humble_bundle_keys.models import GameKey
from humble_bundle_keys.site_session import (
    LIBRARY_URL,
    account_email,
    flight_records,
    library_action,
    script_urls,
)


def query_form(page, limit=100):
    return {"_1_requestId": str(page), "_1_query": "", "_1_sort": "mostRecent",
            "_1_cursor": str(page * limit), "_1_page": str(page), "_1_limit": str(limit),
            "_1_hideRevealed": "false", "_1_hideExpired": "false", "_1_dateExpires": "",
            "0": json.dumps([{"results": [], "pagination": {
                "page": 1, "totalPages": 1, "totalDocs": 0}, "success": True}, "$K1"])}


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
        self.slots = asyncio.Semaphore(3)
        self.action = None

    async def get_text(self, url):
        async with self.slots:
            response = await self.context.request.get(url, timeout=30_000)
            try:
                if response.status != 200:
                    raise ApiError(f"新版官网读取失败（HTTP {response.status}）；旧库存保留。")
                return await response.text()
            finally:
                await response.dispose()

    async def query(self, page):
        async with self.slots:
            for attempt in range(3):
                response = await self.context.request.post(
                    LIBRARY_URL, headers={"Next-Action": self.action,
                                          "Accept": "text/x-component",
                                          "Origin": "https://www.humblebundle.com"},
                    multipart=query_form(page), timeout=30_000)
                try:
                    if response.status in {429, 500, 502, 503, 504} and attempt < 2:
                        await asyncio.sleep(2 ** (attempt + 1))
                        continue
                    if response.status != 200:
                        raise ApiError(f"新版库查询失败（HTTP {response.status}）；旧库存保留。")
                    result = query_result(await response.text())
                    if result["pagination"]["page"] != page:
                        raise ApiUnsupported("新版库返回了错误页码；旧库存保留。")
                    return result
                finally:
                    await response.dispose()

    async def run(self):
        document = await self.get_text(LIBRARY_URL)
        identity = hashlib.sha256(account_email(document).encode()).hexdigest()
        if identity != self.account:
            raise ApiError("新版库账号与当前库存不一致；未更新库存，请重新登录。")
        urls = script_urls(document)
        for offset in range(0, len(urls), 4):
            sources = await asyncio.gather(*(self.get_text(url) for url in urls[offset:offset + 4]))
            self.action = next((value for source in sources
                                if (value := library_action(source))), None)
            if self.action:
                break
        if not self.action:
            raise ApiUnsupported("未识别官网新版只读查询接口；旧库存保留。")
        first = await self.query(1)
        total = first["pagination"]["totalPages"]
        if total > 2000:
            raise ApiUnsupported("官网返回的分页数量异常；旧库存保留。")
        items = list(first["results"])
        self.update(f"新版游戏库：读取 1 / {max(1, total)} 页，每页最多 100 条。")
        for start in range(2, total + 1, 3):
            pages = await asyncio.gather(*(self.query(n)
                                           for n in range(start, min(start + 3, total + 1))))
            for page in pages:
                if page["pagination"]["totalDocs"] != first["pagination"]["totalDocs"]:
                    raise ApiError("扫描期间官网库存发生变化，请重新扫描；旧库存保留。")
                items.extend(page["results"])
            self.update(f"新版游戏库：读取 {min(start + 2, total)} / {total} 页。")
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
        warnings = ["Humble 官网正在迁移历史订单：新版扫描会保留旧库存，不自动删除缺失记录。",
                    "新版刮取只处理官网已分配的独立 Steam Key；"
                    "需选择平台、游戏或额度的分组请在官网操作。"]
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
            warnings.append(f"发现 {len(groups)} 个需在官网核查选择规则的分组，已列入待核查；"
                            "扫描未自动选择或领取。")
        if duplicate_count:
            warnings.append(f"官网分页含 {duplicate_count} 条完全重复记录，已去重；"
                            "保留所有旧库存，可稍后重扫核对迁移变动。")
        self.publish({"rows": merge_rows(self.previous.get("rows", []), fresh),
                      "memberships": memberships, "warnings": warnings,
                      "partial": True, "backend": "humble_next", "upstream_total": len(items),
                      "scanned_at": datetime.now(timezone.utc).isoformat()})
        self.log("new_library_scan_complete", rows=len(fresh), count=len(items),
                 source="humble_next", reason="legacy_inventory_retained_during_migration")
