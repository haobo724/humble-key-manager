"""Local successful activation marks grouped by a hashed Humble account identity."""
import hashlib
import json
import re

from humble_bundle_keys.auth import KEYS_URL
from humble_bundle_keys.steam import atomic_json, key_digest, utc_now


def identify_account(context):
    page = context.new_page()
    try:
        page.goto(KEYS_URL, wait_until="domcontentloaded", timeout=30_000)
        if "/home/keys" not in page.url or "/login" in page.url:
            raise ValueError("请先登录 Humble，才能保存账户激活标记。")
        email = re.search(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", page.title())
        if not email:
            raise ValueError("无法识别 Humble 账号，请重新登录。")
        return hashlib.sha256(email.group().strip().lower().encode()).hexdigest()
    finally:
        page.close()


class HBActivationState:
    def __init__(self, directory):
        self.path = directory / "hb-activations.json"
        self.data = {"current_account": None, "accounts": {}, "legacy_imported_account": None}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

    def bind(self, identity, rows, steam):
        self.data["current_account"] = identity
        marks = self.data["accounts"].setdefault(identity, {})
        # Legacy receipts are only attached to keys present in this account's inventory.
        for row in rows:
            if row.get("platform") != "steam" or not row.get("key"):
                continue
            digest = key_digest(row["key"])
            for steamid, results in steam.data["results"].items():
                result = results.get(digest)
                if (result and result["status"] == "activated" and
                        (result.get("humble_account") == identity or
                         (not result.get("humble_account") and
                          self.data["legacy_imported_account"] in {None, identity}))):
                    marks.setdefault(digest, {"time": result["time"], "steamid": steamid,
                                              "game": row["game_title"]})
        if not self.data["legacy_imported_account"]:
            self.data["legacy_imported_account"] = identity
        atomic_json(self.path, self.data)

    def record(self, row, steamid):
        identity = self.data["current_account"]
        if not identity:
            raise ValueError("Humble 账户未识别，无法保存激活标记。")
        self.data["accounts"].setdefault(identity, {})[key_digest(row["key"])] = {
            "time": utc_now(), "steamid": steamid, "game": row["game_title"]}
        atomic_json(self.path, self.data)

    def annotate(self, rows):
        marks = self.data["accounts"].get(self.data["current_account"], {})
        for row in rows:
            mark = marks.get(key_digest(row["key"])) if row.get("key") else None
            row["system_activated"] = bool(mark) and row.get("platform") == "steam"
            row["system_activation_time"] = mark["time"] if row["system_activated"] else None
        return rows
