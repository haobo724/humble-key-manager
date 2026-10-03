"""Steam sessions, exact AppID ownership checks and explicit batch activation.

Key usage is only known from an activation response, never from game ownership.
No passwords, cookies, raw activation keys or raw receipts enter the logs.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

REGISTER_URL = "https://store.steampowered.com/account/registerkey/?l=english"
LOGIN_URL = "https://store.steampowered.com/login/?redir=account/registerkey&redir_ssl=1"
TERMINAL = {"activated", "activated_elsewhere", "duplicate", "invalid", "uncertain"}
RESULTS = {9: "already_owned", 14: "invalid", 15: "duplicate", 53: "rate_limited",
           13: "region_restricted", 24: "missing_base_game", 36: "requires_ps3"}
KEY_PATTERN = re.compile(r"^[A-Za-z0-9]{4,8}(?:-[A-Za-z0-9]{4,8}){2,4}$")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def key_digest(key):
    return hashlib.sha256(key.strip().upper().encode()).hexdigest()


def row_id(row):
    # A changed key creates a new selection identity, preventing stale activation plans.
    values = [row.get(k, "") for k in ("humble_url", "game_title", "platform", "key")]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:24]


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def parse_activation(data):
    if not isinstance(data, dict):
        return "uncertain"
    if data.get("success") == 1:
        return "activated"
    details = data.get("purchase_result_details")
    return RESULTS.get(details, "uncertain") if type(details) is int else "uncertain"


def hydrate_app_ids(rows, directory):
    """Upgrade old inventories using already-downloaded Humble metadata, without requests."""
    from humble_bundle_keys.api import _extract_tpk
    from humble_bundle_keys.async_inventory import order_tpks

    mapping = {}
    for path in (directory / "orders-cache").glob("*.json"):
        try:
            order = json.loads(path.read_text(encoding="utf-8"))
            for tpk in order_tpks(order):
                game = _extract_tpk(tpk, order)
                if game.steam_app_id:
                    identity = (game.humble_url, game.game_title, game.platform)
                    mapping.setdefault(identity, set()).add(game.steam_app_id)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    for row in rows:
        if row.get("steam_app_id") or row.get("platform") != "steam":
            continue
        values = mapping.get(tuple(row.get(k, "") for k in
                                   ("humble_url", "game_title", "platform")), set())
        if len(values) == 1:
            row["steam_app_id"] = next(iter(values))


class SteamState:
    def __init__(self, directory: Path):
        self.path = directory / "steam.json"
        self.data = {"account": None, "library": None, "results": {}}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

    def save(self):
        atomic_json(self.path, self.data)

    def account(self):
        return self.data.get("account")

    def set_account(self, steamid, name):
        if not self.account() or self.account()["steamid"] != steamid:
            self.data["library"] = None
        self.data["account"] = {"steamid": steamid, "name": name}
        self.save()

    def library(self):
        library = self.data.get("library")
        if library and self.account() and library.get("steamid") == self.account()["steamid"]:
            return library
        return None

    def set_library(self, apps):
        if not self.account():
            raise RuntimeError("请先登录 Steam。")
        self.data["library"] = {"steamid": self.account()["steamid"],
                                "apps": sorted(set(apps)), "synced_at": utc_now()}
        self.save()

    def result(self, key):
        if not key or not self.account():
            return None
        return self.data["results"].get(self.account()["steamid"], {}).get(key_digest(key))

    def record(self, key, status, app_id=None):
        results = self.data["results"].setdefault(self.account()["steamid"], {})
        results[key_digest(key)] = {"status": status, "time": utc_now()}
        if status == "activated" and app_id and self.library():
            self.data["library"]["apps"] = sorted(set(self.library()["apps"]) | {app_id})
        self.save()

    def annotate(self, rows):
        library = self.library()
        apps = set(library["apps"]) if library else set()
        annotated = []
        for original in rows:
            row = original.copy()
            row["id"] = row_id(row)
            app_id = row.get("steam_app_id")
            row["steam_ownership"] = "not_applicable"
            if row.get("platform") == "steam":
                row["steam_ownership"] = ("owned" if app_id in apps else "not_owned") \
                    if library and app_id else "unknown"
            result = self.result(row.get("key", "")) if row.get("platform") == "steam" else None
            # A successfully consumed key cannot become unused by switching the target account.
            if not result and row.get("key") and row.get("platform") == "steam":
                digest = key_digest(row["key"])
                for results in self.data["results"].values():
                    previous = results.get(digest)
                    if previous and previous["status"] in {"activated", "duplicate", "uncertain"}:
                        result = {**previous, "status": "activated_elsewhere"
                                  if previous["status"] == "activated" else previous["status"]}
                        break
            row["activation_status"] = result["status"] if result else "unknown"
            row["activation_time"] = result["time"] if result else None
            # Already-owned responses say nothing about whether the particular key was consumed.
            if result and result["status"] in {"activated", "already_owned"}:
                row["steam_ownership"] = "owned"
            annotated.append(row)
        return annotated

    def summary(self):
        library = self.library()
        return {"account": self.account(), "synced_at": library["synced_at"] if library else None,
                "owned_count": len(library["apps"]) if library else None}


def activation_candidates(rows, selected_ids, steam):
    if not steam.account() or not steam.library():
        raise ValueError("请先登录 Steam 并同步游戏库，再预览激活清单。")
    ids = set(selected_ids)
    annotated = steam.annotate(rows)
    if ids - {r["id"] for r in annotated}:
        raise ValueError("选择记录已变化，请刷新并重新选择。")
    candidates, skipped, seen = [], [], set()
    for row in annotated:
        if row["id"] not in ids:
            continue
        key = row.get("key", "").strip()
        reason = None
        if row.get("platform") != "steam" or not key:
            reason = "非 Steam Key 或尚未刮取"
        elif row.get("deadline_state") == "expired":
            reason = "已过期"
        elif row["steam_ownership"] == "owned":
            reason = "当前 Steam 库已拥有"
        elif row["activation_status"] in TERMINAL:
            reason = "已有明确结果或上次提交结果不确定，请核查"
        elif not KEY_PATTERN.fullmatch(key):
            reason = "Key 格式不支持"
        elif key_digest(key) in seen:
            reason = "重复 Key"
        if reason:
            skipped.append({"id": row["id"], "game_title": row["game_title"], "reason": reason})
        else:
            seen.add(key_digest(key))
            candidates.append(row)
    return candidates, skipped


def account_from_context(context, page):
    if urlparse(page.url).hostname != "store.steampowered.com":
        return None
    if page.locator("#account_pulldown").count() == 0:
        return None
    for cookie in context.cookies("https://store.steampowered.com"):
        if cookie["name"] == "steamLoginSecure":
            steamid = unquote(cookie["value"]).split("||", 1)[0]
            if re.fullmatch(r"\d{17}", steamid):
                return {"steamid": steamid,
                        "name": page.locator("#account_pulldown").inner_text().strip()}
    return None


def steam_context(pw, directory, channel, force_login=False, update=None, stop=None):
    """Only an explicit Steam login action opens the manual sign-in flow."""
    state_path = directory / "steam-storage-state.json"
    browser = pw.chromium.launch(headless=False, channel=channel)
    try:
        context = browser.new_context(
            storage_state=str(state_path) if state_path.exists() and not force_login else None)
        page = context.new_page()
        page.goto(LOGIN_URL if force_login else REGISTER_URL, wait_until="domcontentloaded",
                  timeout=30_000)
        if force_login:
            if update:
                update("请在 Steam 官方窗口扫码或登录并完成 Steam Guard；成功后会自动保存会话。")
            # User enters credentials and completes Steam Guard; no password is read by this app.
            for _ in range(300):
                if stop and stop.is_set():
                    raise RuntimeError("Steam 登录已停止。")
                if account_from_context(context, page):
                    break
                page.wait_for_timeout(1000)
            page.goto(REGISTER_URL, wait_until="domcontentloaded", timeout=30_000)
        account = account_from_context(context, page)
        if not account:
            raise RuntimeError("Steam 会话无效，请点击「Steam 登录」。")
        context.storage_state(path=str(state_path))
        return browser, context, page, account
    except Exception:
        browser.close()
        raise


def sync_library(page, steam):
    raw = page.evaluate("""async () => {
      const r = await fetch('/dynamicstore/userdata/?l=english',
                            {cache:'no-store',signal:AbortSignal.timeout(25000)});
      return {status:r.status, data:r.ok ? await r.json() : null};
    }""")
    data = raw.get("data")
    if raw.get("status") != 200 or not isinstance(data, dict) \
            or not isinstance(data.get("rgOwnedApps"), list):
        raise RuntimeError("Steam 库读取失败；保留上次库记录。")
    apps = data["rgOwnedApps"]
    if any(not isinstance(a, int) or isinstance(a, bool) or a <= 0 for a in apps):
        raise RuntimeError("Steam 库格式变化；保留上次库记录。")
    steam.set_library(apps)


def submit_key(page, key, session):
    # Timeout means uncertain, never unused: Steam may already have consumed the key.
    return page.evaluate("""async ({key, session}) => {
      const r = await fetch('/account/ajaxregisterkey/', {
        method:'POST', signal:AbortSignal.timeout(25000),
        headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                 'X-Requested-With':'XMLHttpRequest'},
        body:new URLSearchParams({product_key:key, sessionid:session}).toString()
      });
      const text = await r.text(); let data = null;
      try {data = JSON.parse(text)} catch {}
      return {status:r.status, data};
    }""", {"key": key, "session": session})


def activate_batch(page, context, steam, rows, selected_ids, inventory):

    candidates, skipped = activation_candidates(rows, selected_ids, steam)
    account = account_from_context(context, page)
    if not account or account["steamid"] != steam.account()["steamid"]:
        raise RuntimeError("Steam 账号与预览不一致，请重新登录并预览。")
    session = next((c["value"] for c in context.cookies("https://store.steampowered.com")
                    if c["name"] == "sessionid"), None)
    if not session:
        raise RuntimeError("Steam 会话缺失，请重新登录。")
    succeeded = 0
    inventory.log("steam_activation_started", candidates=len(candidates), skipped=len(skipped))
    for index, row in enumerate(candidates):
        if inventory.stop_event.is_set():
            break
        if steam.annotate([row])[0]["steam_ownership"] == "owned":
            inventory.log("steam_activation_skipped", game=row["game_title"], reason="now_owned")
            continue
        inventory.update(f"Steam 激活 {index + 1}/{len(candidates)}：{row['game_title']}")
        key = row["key"]
        # Persist before submission. A timeout or process crash must not trigger an automatic retry.
        steam.record(key, "uncertain")
        inventory.touch()
        try:
            response = submit_key(page, key, session)
            status = "rate_limited" if response["status"] == 429 else \
                parse_activation(response["data"]) if response["status"] == 200 else "uncertain"
        except Exception as exc:
            status = "uncertain"
            inventory.log("steam_activation_transport_failed", game=row["game_title"],
                          error_type=type(exc).__name__)
        steam.record(key, status, row.get("steam_app_id"))
        inventory.touch()
        inventory.log("steam_activation_result", game=row["game_title"], status=status)
        succeeded += status == "activated"
        if status in {"rate_limited", "uncertain"}:
            inventory.update("Steam 限流或提交结果不确定，已停止；请稍后核查，不自动重试。")
            return
        if inventory.stop_event.wait(5):
            break
    inventory.update(f"Steam 激活结束：成功 {succeeded}；详细结果已保存到列表。")
    inventory.log("steam_activation_complete", succeeded=succeeded)
