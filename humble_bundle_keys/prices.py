"""Optional ITAD prices: exact Steam AppID, Steam only, China only.

Settings stay in the local data directory; only public game IDs leave the app.
"""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from humble_bundle_keys.steam import atomic_json


def request(path, key, body=None):
    req = Request("https://api.isthereanydeal.com" + path,
                  data=None if body is None else json.dumps(body).encode(),
                  headers={"ITAD-API-Key": key, "Content-Type": "application/json",
                           "User-Agent": "HumbleKeyManager"})
    try:
        with urlopen(req, timeout=25) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("API Key 无效或已过期；请填写 API Key，不是 Client Secret。") from None
        if exc.code == 429:
            raise ValueError("ITAD 请求限流，请稍后重试；已取得的价格已保存。") from None
        raise ValueError(f"ITAD 返回 HTTP {exc.code}，请稍后重试。") from None
    except (URLError, OSError, ValueError):
        raise ValueError("ITAD 网络请求失败或响应无法读取，请稍后重试。") from None


def steam_low(lows):
    """Never label another store/currency as a Steam China price."""
    for low in lows:
        price = low.get("price") or {}
        amount = price.get("amount")
        if (low.get("shop", {}).get("id") == 61 and price.get("currency") == "CNY"
                and type(amount) in (int, float) and math.isfinite(amount) and amount >= 0):
            return {"amount": amount, "currency": "CNY", "country": "CN",
                    "timestamp": low.get("timestamp"), "source": "IsThereAnyDeal"}
    return None


class PriceStore:
    def __init__(self, directory):
        self.directory = directory
        self.lock = threading.RLock()
        self.key = self.load("itad-settings.json").get("api_key", "")
        self.cache = self.load("itad-prices.json")
        self.busy = False
        self.message = "已加载本地价格缓存。" if self.cache else "价格尚未刷新。"
        self.error = ""

    def load(self, name):
        try:
            data = json.loads((self.directory / name).read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def configure(self, key):
        if not isinstance(key, str) or len(key) > 256:
            raise ValueError("请输入 API Key。")
        key = key.strip()
        with self.lock:
            if self.busy:
                raise ValueError("价格正在刷新，请完成后再修改配置。")
            if key:
                request("/games/lookup/v1?appid=620", key)
            atomic_json(self.directory / "itad-settings.json", {"api_key": key})
            self.key = key
            self.error = ""
            self.message = "API Key 已验证并保存。" if key else "API Key 已清除；价格缓存保留。"

    def summary(self):
        with self.lock:
            return {"configured": bool(self.key), "busy": self.busy,
                    "message": self.message, "error": self.error,
                    "cached_count": len(self.cache)}

    def annotate(self, rows):
        with self.lock:
            result = []
            for row in rows:
                item = self.cache.get(str(row.get("steam_app_id")), {})
                applicable = row.get("platform") == "steam"
                low = item.get("low") if applicable else None
                result.append({**row, "steam_cn_low": low,
                               "steam_cn_low_amount": low.get("amount") if low else None,
                               "steam_cn_low_date": low.get("timestamp") if low else None,
                               "price_checked_at": item.get("checked_at") if applicable else None,
                               "price_status": (item.get("status", "not_loaded") if applicable
                                                and row.get("steam_app_id") else
                                                "missing_appid" if applicable
                                                else "not_applicable")})
            return result

    def start(self, app_ids, changed):
        with self.lock:
            if self.busy:
                raise ValueError("价格正在刷新。")
            if not self.key:
                raise ValueError("请先配置 API Key。")
            ids = sorted({str(a) for a in app_ids if a and str(a).isdigit()})
            if not ids:
                raise ValueError("尚无可查询的 Steam AppID，请先扫描。")
            self.busy, self.error = True, ""
            self.message = f"正在查询 {len(ids)} 个游戏的 Steam 国区史低…"
            key = self.key
        threading.Thread(target=self.refresh, args=(ids, key, changed), daemon=True).start()

    def refresh(self, ids, key, changed):
        try:
            # Batches avoid a separate lookup request for every game.
            for offset in range(0, len(ids), 200):
                batch = ids[offset:offset + 200]
                mapping = request("/lookup/id/shop/61/v1", key, [f"app/{a}" for a in batch])
                games = {a: mapping.get(f"app/{a}") for a in batch}
                for gid in games.values():
                    if gid is not None:
                        UUID(gid)
                gids = list({g for g in games.values() if g})
                response = (request("/games/storelow/v2?country=CN&shops=61", key, gids)
                            if gids else [])
                lows = {entry["id"]: steam_low(entry.get("lows", [])) for entry in response}
                checked = datetime.now(timezone.utc).isoformat()
                with self.lock:
                    for app, gid in games.items():
                        # An omitted item is not evidence that a previous price disappeared.
                        if gid and gid not in lows:
                            continue
                        low = lows.get(gid)
                        self.cache[app] = {"low": low, "checked_at": checked,
                                           "status": "ok" if low else "no_data"}
                    atomic_json(self.directory / "itad-prices.json", self.cache)
                    self.message = f"价格刷新：已处理 {min(offset + 200, len(ids))} / {len(ids)}。"
                changed()
            with self.lock:
                self.message = "Steam 国区史低刷新完成；没有记录的游戏显示暂无数据。"
        except ValueError as exc:
            with self.lock:
                self.error = (str(exc) if "ITAD" in str(exc) or "API Key" in str(exc)
                              else "价格响应格式异常。")
        except Exception:
            with self.lock:
                self.error = "价格刷新未完成，之前的缓存已保留。"
        finally:
            with self.lock:
                self.busy = False
            changed()
