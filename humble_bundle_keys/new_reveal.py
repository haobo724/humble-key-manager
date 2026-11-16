"""Explicit, single-submit redemption of unambiguous standalone Steam entitlements."""
import json
import re
from datetime import datetime, timezone

from humble_bundle_keys.api import ApiError, ApiUnsupported, normalise_key
from humble_bundle_keys.diagnostics import error_fields
from humble_bundle_keys.operation_report import work_id
from humble_bundle_keys.site_session import (
    LIBRARY_URL,
    flight_records,
    objects,
    redemption_action,
    script_urls,
)
from humble_bundle_keys.steam import row_id

REASONS = {"expired": "已过期", "out_of_stock": "Key 暂时缺货，补货后可重试",
           "out_of_stock_notified": "Key 暂时缺货，官网将通知补货",
           "sold_out": "Key 暂时缺货", "selection_already_made": "该分组已有领取选择，请在官网核查",
           "region_blocked": "官网提示地区限制", "not_available_yet": "尚未开放领取"}


class NewSteamRevealer:
    def __init__(self, context):
        self.context = context
        self.action = None

    def text(self, url):
        response = self.context.request.get(url, timeout=30_000)
        try:
            if response.status != 200:
                raise ApiError(f"新版领取页面读取失败（HTTP {response.status}）。")
            return response.text()
        finally:
            response.dispose()

    def inspect(self, row):
        item_id = row.get("modern_item_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", item_id):
            raise ApiUnsupported("缺少新版库存标识，请先重新扫描。")
        document = self.text(f"{LIBRARY_URL}/content/{item_id}")
        data = list(objects(flight_records(document)))
        if any(type(obj.get("maxRedemptions")) is int for obj in data):
            raise ApiUnsupported("该项目存在选择额度，请在官网选择；未提交领取。")
        items = [obj["userLibraryItem"] for obj in data
                 if isinstance(obj.get("userLibraryItem"), dict)
                 and isinstance(obj["userLibraryItem"].get("entitlements"), dict)]
        if not items or any(obj != items[0] for obj in items):
            raise ApiUnsupported("无法唯一核对新版领取项目；未提交领取。")
        keys = items[0]["entitlements"].get("keys", [])
        if len(keys) != 1 or not isinstance(keys[0], dict):
            raise ApiUnsupported("该项目需要选择平台或权益，请在官网领取；未提交领取。")
        entitlement = keys[0]
        if (entitlement.get("id") != row.get("modern_entitlement_id")
                or entitlement.get("keyIntegrationId") != "steam"
                or entitlement.get("type") != "steam"):
            raise ApiUnsupported("领取标识或平台与扫描记录不一致；未提交领取。")
        return document, entitlement

    def reveal(self, row):
        document, entitlement = self.inspect(row)
        key = normalise_key((entitlement.get("redemptionData") or {}).get("keyFormatted"))
        if key:
            return {"status": "recovered", "key": key,
                    "time": entitlement.get("redeemedAt")}
        if entitlement.get("expired"):
            return {"status": "expired"}
        if entitlement.get("outOfStock"):
            return {"status": "out_of_stock"}
        if not entitlement.get("redeemable") or entitlement.get("giftStatus") == "gifted":
            return {"status": "not_available_yet"}
        if not self.action:
            for url in script_urls(document):
                self.action = redemption_action(self.text(url))
                if self.action:
                    break
        if not self.action:
            raise ApiUnsupported("无法识别新版领取接口；未提交领取。")
        # Exactly one mutation. A timeout or unrecognised reply is never retried.
        response = None
        try:
            response = self.context.request.post(
                f"{LIBRARY_URL}/content/{row['modern_item_id']}",
                headers={"Next-Action": self.action, "Accept": "text/x-component",
                         "Origin": "https://www.humblebundle.com",
                         "Content-Type": "text/plain;charset=UTF-8"},
                data=json.dumps([entitlement["id"], "steam", "$undefined"]), timeout=30_000)
            if response.status != 200:
                return {"status": "uncertain"}
            replies = [obj for obj in flight_records(response.text()) if isinstance(obj, dict)
                       and isinstance(obj.get("success"), bool)]
            if len(replies) != 1:
                return {"status": "uncertain"}
            result = replies[0]
            key = normalise_key(result.get("key"))
            if result["success"] and key:
                return {"status": "success", "key": key,
                        "time": datetime.now(timezone.utc).isoformat()}
            kind = result.get("errorKind")
            return {"status": kind if kind in REASONS else "uncertain"}
        except Exception:
            return {"status": "uncertain"}
        finally:
            if response is not None:
                response.dispose()


def reveal_new_library(context, inventory, selected_ids=None):
    selected = set(selected_ids) if selected_ids is not None else None
    rows = [row.copy() for row in inventory.snapshot["rows"] if row.get("modern_entitlement_id")
            and not row.get("key") and row.get("platform") == "steam"
            and (selected is None or row_id(row) in selected)]
    if not rows:
        raise ValueError("所选记录中没有新版官网已分配的未刮取 Steam Key，请先扫描或打开官网核查。")
    inventory.begin_report("reveal-selected" if selected is not None else "reveal")
    client = NewSteamRevealer(context)
    for row in rows:
        identity = work_id(row["modern_item_id"], row["game_title"], row["platform"])
        inventory.report_item(identity, row["game_title"], row.get("bundle_name", ""))
    for row in rows:
        if inventory.stop_event.is_set():
            break
        identity = work_id(row["modern_item_id"], row["game_title"], row["platform"])
        inventory.report_phase("新版官网核对并刮取", row["game_title"], row.get("bundle_name", ""))
        inventory.update(f"新版官网核对并刮取：{row['game_title']}")
        try:
            result = client.reveal(row)
            if result["status"] in {"success", "recovered"}:
                with inventory.lock:
                    snapshot = {**inventory.snapshot,
                                "rows": [r.copy() for r in inventory.snapshot["rows"]]}
                    for current in snapshot["rows"]:
                        if current.get("modern_entitlement_id") == row["modern_entitlement_id"]:
                            current["key"] = result["key"]
                            current["redeemed_on_humble"] = True
                            current["upstream_out_of_stock"] = False
                            current.pop("unavailable_status", None)
                            current.pop("unavailable_reason", None)
                            if result.get("time"):
                                current["revealed_at"] = result["time"]
                    inventory.publish(snapshot)
                inventory.report_item(identity, row["game_title"], row.get("bundle_name", ""),
                                      "success")
                inventory.log("new_library_key_revealed", game=row["game_title"],
                              bundle=row.get("bundle_name", ""), source="humble_next")
                if inventory.stop_event.wait(3):
                    break
            elif result["status"] in REASONS:
                if result["status"] in {"expired", "out_of_stock", "out_of_stock_notified",
                                        "sold_out"}:
                    with inventory.lock:
                        snapshot = {**inventory.snapshot,
                                    "rows": [r.copy() for r in inventory.snapshot["rows"]]}
                        for current in snapshot["rows"]:
                            if current.get("modern_entitlement_id") == row["modern_entitlement_id"]:
                                current["unavailable_status"] = (
                                    "expired" if result["status"] == "expired"
                                    else "key_temporarily_exhausted")
                                current["unavailable_reason"] = REASONS[result["status"]]
                                current["upstream_expired"] = result["status"] == "expired"
                                current["upstream_out_of_stock"] = result["status"] != "expired"
                        inventory.publish(snapshot)
                inventory.report_item(identity, row["game_title"], row.get("bundle_name", ""),
                                      "skipped", REASONS[result["status"]])
            else:
                inventory.report_item(identity, row["game_title"], row.get("bundle_name", ""),
                                      "failed", "领取结果不确定，已停止；请核查官网，未重复提交")
                inventory.report_issue("新版领取结果不确定，其余项目未处理。")
                return
        except ApiUnsupported as exc:
            inventory.report_item(identity, row["game_title"], row.get("bundle_name", ""),
                                  "skipped", str(exc))
            inventory.log("new_library_reveal_skipped", **error_fields(exc))
        except Exception as exc:
            inventory.report_item(identity, row["game_title"], row.get("bundle_name", ""),
                                  "failed", type(exc).__name__)
            inventory.log("new_library_reveal_failed", **error_fields(exc))
            inventory.report_issue("新版页面核对失败，其余项目未处理。")
            return
    inventory.update("新版刮取任务已结束，已取得的 Key 和逐项结果已保存。")
