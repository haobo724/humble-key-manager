"""Local inventory UI with read-only scans and explicit key revelation."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import secrets
import socket
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

from humble_bundle_keys.async_inventory import ReadOnlyScanner
from humble_bundle_keys.auth import AuthOptions, get_authenticated_context
from humble_bundle_keys.availability import BLOCKED, REASONS, AvailabilityState
from humble_bundle_keys.browser_choice import derive_membership_slug
from humble_bundle_keys.choice_policy import choice_policy, upgrade_membership
from humble_bundle_keys.deadlines import annotate_rows
from humble_bundle_keys.hb_activation import HBActivationState, read_account
from humble_bundle_keys.month_claim import claim_months, month_slug, preview_months
from humble_bundle_keys.operation_report import OperationReport
from humble_bundle_keys.reveal import preview_reveal, reveal_all
from humble_bundle_keys.steam import (
    SteamState,
    activate_batch,
    activation_candidates,
    hydrate_app_ids,
    row_id,
    steam_context,
    sync_library,
)

DATA_DIR = Path.cwd() / ".humble-bundle-keys" / "web"
HTML_PATH = Path(__file__).with_name("web_ui.html")


def available_browser_channel() -> str | None:
    if getattr(sys, "frozen", False):
        return None  # The Windows release includes its own Chromium.
    edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    edge64 = Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe")
    return "msedge" if edge.is_file() or edge64.is_file() else None


def safe_error(exc: Exception) -> str:
    """Expose useful diagnostics without private URLs or activation codes."""
    detail = re.sub(r"https?://[^\s\"'<>]+", "[网址已隐藏]", str(exc))
    detail = re.sub(r"\b[A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8}){1,4}\b", "[Key 已隐藏]", detail)
    return f"{type(exc).__name__}: {detail}"[:2000]


def membership_url(product: dict) -> str | None:
    if product.get("category") != "subscriptioncontent":
        return None
    slug = derive_membership_slug(product)
    if not slug:
        return None
    if slug.startswith("https://www.humblebundle.com/membership/"):
        slug = urlparse(slug).path.rsplit("/", 1)[-1]
    if any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in slug):
        return None
    return f"https://www.humblebundle.com/membership/{slug}"


def is_legacy_monthly(product: dict) -> bool:
    machine = product.get("machine_name", "").lower()
    return machine.endswith("_monthly") and not product.get("choice_url")


def describe_membership(order: dict, cards: list[dict], url: str) -> dict:
    policy = choice_policy(order.get("product") or {}, url)
    remaining = order.get("choices_remaining")
    if isinstance(remaining, str) and remaining.isdigit():
        remaining = int(remaining)
    if not isinstance(remaining, int) or isinstance(remaining, bool):
        remaining = None
    if policy == "all_games":
        remaining = None
    unclaimed = [c["title"] for c in cards if not c["claimed"]]
    if not cards:
        state = "unknown"
    elif policy != "all_games" and remaining == 0 and unclaimed:
        state = "exhausted"
    elif unclaimed:
        state = "pending"
    else:
        state = "complete"
    return {
        "bundle_name": (order.get("product") or {}).get("human_name", "月包"),
        "url": url, "state": state, "choices_remaining": remaining,
        "unclaimed_titles": unclaimed, "total_games": len(cards),
        "claimed_games": sum(bool(c["claimed"]) for c in cards),
        "choice_policy": policy,
    }


def reconcile_rows(rows: list[dict], memberships: list[dict]) -> list[dict]:
    """Key visibility and unchosen entitlement are different states."""
    for row in rows:
        row["status"] = "revealed" if row["key"] else "unrevealed"
    for month in memberships:
        if month["state"] != "pending":
            continue
        pending = {s.casefold().strip() for s in month["unclaimed_titles"]}
        for row in rows:
            if (not row["key"] and row["bundle_name"] == month["bundle_name"]
                    and row["game_title"].casefold().strip() in pending):
                row["status"] = "pending_choice"
    return rows


class Inventory:
    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = threading.RLock()
        self.busy = False
        self.action = None
        self.stop_event = threading.Event()
        self.message = "尚未扫描。点击登录或扫描开始。"
        self.error = ""
        self.revision = 0
        self.logs = []
        self.activation_plan = None
        self.month_plan = None
        self.action_options = {}
        self.logger = logging.getLogger(f"humble-web.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        directory.mkdir(parents=True, exist_ok=True)
        self.report = OperationReport.load(directory / "operation-report.json")
        self.steam = SteamState(directory)
        self.hb_activation = HBActivationState(directory)
        self.availability = AvailabilityState(directory)
        handler = RotatingFileHandler(directory / "scan.log", maxBytes=2_000_000,
                                      backupCount=2, encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(handler)
        log_path = directory / "scan.log"
        if log_path.exists():
            try:
                self.logs = [json.loads(line) for line in
                             log_path.read_text(encoding="utf-8").splitlines()[-500:]]
            except (OSError, ValueError):
                pass
        self.snapshot = {"rows": [], "memberships": [], "scanned_at": None, "warnings": []}
        path = directory / "inventory.json"
        if path.exists():
            try:
                self.snapshot = json.loads(path.read_text(encoding="utf-8"))
                annotate_rows(self.snapshot["rows"])
                for month in self.snapshot.get("memberships", []):
                    upgrade_membership(month)
                reconcile_rows(self.snapshot["rows"], self.snapshot.get("memberships", []))
                self.message = "已加载上次扫描记录；重新扫描可更新状态。"
            except (ValueError, OSError):
                self.message = "上次记录无法读取，请重新扫描。"
        hydrate_app_ids(self.snapshot["rows"], directory)

    def update(self, message: str):
        with self.lock:
            self.message = message

    def log(self, event: str, **fields):
        entry = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        self.logger.info(json.dumps(entry, ensure_ascii=False))
        for handler in self.logger.handlers:
            handler.close()
        with self.lock:
            self.logs.append(entry)
            self.logs = self.logs[-500:]
            account = self.hb_activation.data["current_account"]
            if event in {"month_claim_skipped", "choice_reveal_skipped"} and \
                    fields.get("reason") in REASONS:
                self.availability.record(account, fields["month"], fields["game"],
                                         fields["reason"], time=entry["time"])
                self.revision += 1
            elif event in {"month_key_revealed", "month_key_recovered", "choice_key_revealed"}:
                self.availability.record(account, fields["month"], fields["game"], None)

    def publish(self, result):
        with self.lock:
            reveal_times = {row['key']: row['revealed_at']
                            for row in self.snapshot['rows']
                            if row.get('key') and row.get('revealed_at')}
            for row in result['rows']:
                if row.get('key') in reveal_times:
                    row['revealed_at'] = reveal_times[row['key']]
            for month in result["memberships"]:
                upgrade_membership(month)
            result["rows"] = annotate_rows(reconcile_rows(result["rows"], result["memberships"]))
            temporary = self.directory / "inventory.tmp"
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.directory / "inventory.json")
            with self.lock:
                self.snapshot = result
                self.revision += 1

    def view(self):
        with self.lock:
            snapshot = self.availability.annotate(
                self.snapshot, self.hb_activation.data["current_account"])
            return {**snapshot, "rows": self.hb_activation.annotate(
                        self.steam.annotate(snapshot["rows"])),
                    "steam": self.steam.summary(), "busy": self.busy,
                    "humble_account": self.hb_activation.data["account_labels"].get(
                        self.hb_activation.data["current_account"]),
                    "month_preview": self.month_plan,
                    "operation_report": self.report.view() if self.report else None,
                    "message": self.message, "error": self.error, "revision": self.revision,
                    "action": self.action}

    def bind_humble(self, identity, email=None):
        with self.lock:
            previous = self.hb_activation.data["current_account"]
            if previous and previous != identity:
                self.snapshot = {"rows": [], "memberships": [], "scanned_at": None,
                                 "warnings": []}
                self.activation_plan = self.month_plan = None
                self.publish(self.snapshot)
            self.hb_activation.bind(identity, self.snapshot["rows"], self.steam, email=email)
            self.revision += 1

    def mark_hb_activation(self, row, steamid):
        with self.lock:
            result = self.steam.result(row["key"])
            if result:
                result["humble_account"] = self.hb_activation.data["current_account"]
                self.steam.save()
            self.hb_activation.record(row, steamid)

    def touch(self):
        with self.lock:
            self.revision += 1

    def begin_report(self, action, estimated=False):
        with self.lock:
            if not self.report or self.report.data["status"] != "running":
                self.report = OperationReport(action, estimated)
                self.report.save(self.directory / "operation-report.json")

    def report_phase(self, phase, game="", bundle=""):
        with self.lock:
            if self.report:
                self.report.data.update(phase=phase, current_game=game, current_bundle=bundle)

    def report_item(self, identity, title, bundle="", status=None, reason=""):
        with self.lock:
            if self.report:
                self.report.add(identity, title, bundle)
                if status:
                    self.report.result(identity, status, reason)
                self.report.save(self.directory / "operation-report.json")

    def report_issue(self, reason):
        with self.lock:
            if self.report:
                self.report.issue(reason)
                self.report.save(self.directory / "operation-report.json")

    def report_has(self, identity):
        with self.lock:
            return bool(self.report and identity in self.report.data["items"])

    def finish_report(self, error=None):
        with self.lock:
            if self.report and self.report.data["status"] == "running":
                self.report.finish(stopped=self.stop_event.is_set(), error=error)
                self.report.save(self.directory / "operation-report.json")

    def plan_activation(self, selected_ids):
        with self.lock:
            if not self.hb_activation.data["current_account"]:
                raise ValueError("请先登录或扫描 Humble，识别激活标记所属账号。")
            if self.busy:
                raise ValueError("已有操作正在执行，请结束后再预览。")
            candidates, skipped = activation_candidates(
                self.snapshot["rows"], selected_ids, self.steam)
            self.activation_plan = {"id": secrets.token_urlsafe(24), "revision": self.revision,
                                    "humble_account": self.hb_activation.data["current_account"],
                                    "steamid": self.steam.account()["steamid"],
                                    "selected_ids": [r["id"] for r in candidates]}
            return {"plan_id": self.activation_plan["id"], "revision": self.revision,
                    "account": self.steam.account(), "skipped": skipped,
                    "games": [{"game_title": r["game_title"],
                               "steam_ownership": r["steam_ownership"],
                               "steam_app_id": r.get("steam_app_id")} for r in candidates]}

    def plan_months(self, requests, revision):
        """Freeze the displayed inventory selection without opening Humble pages."""
        from humble_bundle_keys.month_claim import validate_selection
        with self.lock:
            if self.busy or revision != self.revision:
                raise ValueError("列表已变化或有操作进行中，请刷新后重新确认。")
            months = {m["url"]: m for m in self.snapshot["memberships"]}
            targets = []
            for request in requests:
                m = months[request["url"]]
                policy = m.get("choice_policy", "unknown")
                if request.get("all_games") is True and policy != "all_games":
                    raise ValueError("只有无限额月份可以领取全部游戏。")
                titles = (list(m["unclaimed_titles"]) if request.get("all_games") is True
                          else list(request["titles"]))
                marks = self.availability.games(
                    self.hb_activation.data["current_account"], m["url"])
                blocked = {title for title, mark in marks.items() if mark["status"] in BLOCKED}
                if request.get("all_games") is True:
                    titles = [title for title in titles if title not in blocked]
                elif blocked.intersection(titles):
                    raise ValueError("所选游戏包含已过期或需关联账号领取的项目，请取消勾选。")
                if not titles and request.get("all_games") is True:
                    continue
                remaining = m.get("choices_remaining") if policy == "limited" else None
                validate_selection(policy, remaining, titles, m["unclaimed_titles"])
                targets.append({"url": m["url"], "bundle_name": m["bundle_name"],
                                "titles": titles, "choice_policy": policy,
                                "choices_remaining": remaining})
            if not targets:
                raise ValueError("当前列表没有可刮取的游戏。")
            self.month_plan = {"plan_id": secrets.token_urlsafe(24), "revision": self.revision,
                               "targets": targets, "source": "inventory"}
            return self.month_plan

    def start(self, action: str, expected_revision=None, options=None) -> bool:
        with self.lock:
            if self.busy or (expected_revision is not None and expected_revision != self.revision):
                return False
            self.stop_event.clear()
            self.action = action
            self.action_options = options or {}
            if action == "month-preview":
                self.month_plan = None
            self.busy, self.error = True, ""
            if action in {"reveal", "reveal-selected", "claim-months"}:
                self.report = OperationReport(action, estimated=action != "claim-months")
                self.report.save(self.directory / "operation-report.json")
            self.message = "正在连接 Humble；如弹出浏览器，请在官网完成登录。"
            if action.startswith("steam") or action == "activate":
                self.message = "正在连接 Steam…"
        threading.Thread(target=self.run, args=(action,), daemon=True).start()
        return True

    def run(self, action: str):
        report_error = None
        try:
            self.log("operation_started", action=action)
            self.directory.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as pw:
                if action.startswith("steam") or action == "activate":
                    if action == "activate":
                        hb_browser, hb_context = get_authenticated_context(pw, AuthOptions(
                            storage_state_path=self.directory / "storage_state.json",
                            headless=True, browser_channel=available_browser_channel()))
                        try:
                            account = read_account(hb_context)
                            identity = account["identity"]
                            if identity != self.action_options["humble_account"]:
                                self.bind_humble(identity, account["email"])
                                raise ValueError("Humble 账号与预览不同，请重新扫描并预览。")
                            self.bind_humble(identity, account["email"])
                        finally:
                            hb_browser.close()
                    self.run_steam(pw, action)
                    return
                browser, context = get_authenticated_context(pw, AuthOptions(
                    storage_state_path=self.directory / "storage_state.json",
                    headless=False, force_login=action == "login",
                    browser_channel=available_browser_channel(),
                ))
                try:
                    account = read_account(context)
                    self.bind_humble(account["identity"], account["email"])
                    if action == "login":
                        self.update("登录成功，现在可以扫描账户。")
                        return
                    if action == "month-preview":
                        targets = preview_months(context, self, self.action_options["requests"])
                        with self.lock:
                            self.month_plan = {"plan_id": secrets.token_urlsafe(24),
                                               "revision": self.revision, "targets": targets}
                        self.update("月包额度已核对，请确认领取清单。预览未领取任何游戏。")
                        return
                    if action == "claim-months":
                        claim_months(context, self, self.action_options["targets"])
                        return
                    if action in {"reveal", "reveal-selected"}:
                        reveal_all(context, self, self.action_options.get("selected_ids"))
                        if self.stop_event.is_set():
                            self.update("刮取已停止；成功的 Key 已保存。")
                            return
                    session = context.storage_state()
                finally:
                    browser.close()
            asyncio.run(self.scan_async(session, full=action == "full-scan"))
        except Exception as exc:
            detail = safe_error(exc)
            report_error = detail
            self.log("operation_failed", error=detail)
            print(detail, flush=True)
            try:
                (self.directory / "last-error.log").write_text(detail, encoding="utf-8")
            except OSError:
                pass
            with self.lock:
                self.error = detail
                if "Target page, context or browser has been closed" in str(exc):
                    self.error = "登录或扫描窗口提前关闭。请重新登录，并保持窗口打开直到操作完成。"
                self.message = "操作未完成；保留上次扫描记录。"
        finally:
            if action in {"reveal", "reveal-selected", "claim-months"}:
                self.finish_report(report_error)
            with self.lock:
                self.busy = False

    def run_steam(self, pw, action):
        browser, context, page, account = steam_context(
            pw, self.directory, available_browser_channel(), action == "steam-login",
            self.update, self.stop_event)
        try:
            if action == "activate" and account["steamid"] != self.action_options["steamid"]:
                raise RuntimeError("登录账号与激活预览不同，已停止。请重新预览。")
            self.steam.set_account(account["steamid"], account["name"])
            self.touch()
            self.update("正在读取 Steam 库，并按 Humble 提供的 AppID 核对…")
            sync_library(page, self.steam)
            self.touch()
            if action == "activate":
                activate_batch(page, context, self.steam, self.snapshot["rows"],
                               self.action_options["selected_ids"], self)
                # Refresh the server's owned apps after confirmed mutations; keep per-key receipts.
                try:
                    sync_library(page, self.steam)
                    self.touch()
                except Exception:
                    self.log("steam_library_refresh_failed", reason="read_failed")
            else:
                self.update(f"Steam 库核对完成：{len(self.steam.library()['apps'])} 个拥有项目。")
                self.log("steam_library_synced", owned_count=len(self.steam.library()["apps"]))
        finally:
            browser.close()

    async def scan_async(self, session, full=False):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False, channel=available_browser_channel())
            context = await browser.new_context(storage_state=session)
            try:
                scanner = ReadOnlyScanner(
                    context, self.directory, self.publish, self.update, describe_membership,
                    membership_url, previous=self.snapshot, full=full, log=self.log,
                    legacy=is_legacy_monthly,
                )
                await scanner.run()
                self.update(f"扫描完成：{len(self.snapshot['rows'])} 条 Key 记录，"
                            f"{len(self.snapshot['memberships'])} 个 Choice 月包。")
                self.log("scan_complete", rows=len(self.snapshot["rows"]),
                         months=len(self.snapshot["memberships"]),
                         warnings=len(self.snapshot["warnings"]))
            finally:
                await browser.close()


def make_handler(inventory: Inventory, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, value, status=200, mime="application/json; charset=utf-8"):
            body = value.encode("utf-8") if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def local_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.local_host():
                return self.reply({"error": "Invalid host"}, 403)
            if self.path == "/":
                return self.reply(HTML_PATH.read_text(encoding="utf-8").replace(
                    "__TOKEN__", token), mime="text/html; charset=utf-8")
            if self.path == "/api/state" and self.headers.get("X-Local-Token") == token:
                return self.reply(inventory.view())
            if self.path == "/api/logs" and self.headers.get("X-Local-Token") == token:
                with inventory.lock:
                    entries = list(inventory.logs)
                return self.reply(entries)
            if self.path == "/api/reveal-preview" and self.headers.get("X-Local-Token") == token:
                return self.reply(preview_reveal(inventory.view()))
            self.reply({"error": "Not found"}, 404)

        def do_POST(self):
            origin = self.headers.get("Origin")
            expected = f"http://127.0.0.1:{self.server.server_port}"
            if (not self.local_host() or self.headers.get("X-Local-Token") != token
                    or (origin and origin != expected)):
                return self.reply({"error": "Forbidden"}, 403)
            actions = {"/api/login": "login", "/api/scan": "scan",
                       "/api/full-scan": "full-scan", "/api/reveal": "reveal",
                       "/api/steam-login": "steam-login", "/api/steam-sync": "steam-sync",
                       "/api/activate": "activate", "/api/reveal-selected": "reveal-selected"}
            actions.update({"/api/month-preview": "month-preview",
                            "/api/claim-months": "claim-months"})
            if self.path == "/api/stop":
                inventory.stop_event.set()
                return self.reply({"stopping": True})
            if self.path not in actions and self.path != "/api/activation-preview":
                return self.reply({"error": "Not found"}, 404)
            revision = None
            options = None
            if self.path in {"/api/month-preview", "/api/claim-months"}:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 100_000:
                        raise ValueError("清单过大")
                    body = json.loads(self.rfile.read(length))
                    revision = body["revision"]
                    if type(revision) is not int:
                        raise ValueError("请重新预览")
                    if self.path == "/api/month-preview":
                        requests = body["requests"]
                        if not isinstance(requests, list) or not 0 < len(requests) <= 100:
                            raise ValueError("请选择月份和游戏")
                        urls = set()
                        known = {m["url"] for m in inventory.snapshot["memberships"]}
                        for request in requests:
                            url = request["url"]
                            month_slug(url)
                            if url not in known or url in urls:
                                raise ValueError("月份已变化或重复，请重新扫描")
                            urls.add(url)
                            if request.get("all_games") is not True:
                                titles = request["titles"]
                                if (not isinstance(titles, list) or not 0 < len(titles) <= 100
                                        or any(not isinstance(t, str) or not 0 < len(t) <= 500
                                               for t in titles)):
                                    raise ValueError("请选择该月要领取的游戏")
                        return self.reply(inventory.plan_months(requests, revision))
                    else:
                        plan = inventory.month_plan
                        if (body.get("confirm_claim") is not True or not plan
                                or body.get("plan_id") != plan["plan_id"]
                                or revision != plan["revision"]):
                            raise ValueError("请先核对额度并确认月包领取清单")
                        options = {"targets": plan["targets"]}
                except (ValueError, KeyError, TypeError) as exc:
                    return self.reply({"error": safe_error(exc)}, 400)
            if self.path in {"/api/reveal", "/api/reveal-selected", "/api/activate",
                             "/api/activation-preview"}:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 100_000:
                        raise ValueError("bad request size")
                    body = json.loads(self.rfile.read(length))
                    if self.path in {"/api/activation-preview", "/api/reveal-selected"}:
                        ids = body["selected_ids"]
                        if (not isinstance(ids, list) or not 0 < len(ids) <= 2000
                                or any(not isinstance(i, str) or len(i) != 24 for i in ids)):
                            raise ValueError("请选择记录")
                        if self.path == "/api/activation-preview":
                            return self.reply(inventory.plan_activation(ids))
                        known = {row_id(r) for r in inventory.snapshot["rows"]}
                        if set(ids) - known:
                            raise ValueError("选择记录已变化")
                        options = {"selected_ids": ids}
                    if self.path == "/api/activate":
                        plan = inventory.activation_plan
                        if (body.get("confirm_activation") is not True
                                or body.get("accept_ssa") is not True or not plan
                                or body.get("plan_id") != plan["id"]
                                or body.get("revision") != plan["revision"]):
                            raise ValueError("请重新预览并确认目标账号及 Steam 协议")
                        options = plan.copy()
                    revision = body["revision"]
                    if not isinstance(revision, int) or isinstance(revision, bool):
                        raise ValueError("bad revision")
                except (ValueError, KeyError, TypeError):
                    message = "请检查选择清单、登录与库同步状态，并重新预览。"
                    return self.reply({"error": message}, 400)
            if not inventory.start(actions[self.path], revision, options):
                return self.reply({"error": "已有操作正在执行，或清单已更新，请重新预览。"}, 409)
            self.reply({"started": True}, 202)
    return Handler


class LocalWebServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        # Windows otherwise permits multiple servers to bind the same address.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    parser = argparse.ArgumentParser(description="Humble 本地 Key 管理界面")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    server = LocalWebServer(("127.0.0.1", args.port), make_handler(
        Inventory(args.data_dir), secrets.token_urlsafe(32)))
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Humble 管理界面：{url}\n按 Ctrl+C 关闭。", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
