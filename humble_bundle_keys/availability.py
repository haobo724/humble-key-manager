"""Known delivery problems, persisted separately for each Humble account."""
import copy

from humble_bundle_keys.deadlines import annotate_rows
from humble_bundle_keys.steam import atomic_json, utc_now

REASONS = {
    "expired": "已过期，无法领取或兑换",
    "epic_account_link_required": "需关联 Epic 账号领取，不提供可刮取 Key",
    "key_temporarily_exhausted": "Key 暂时缺货，补货后可重试",
}
BLOCKED = {"expired", "epic_account_link_required"}


class AvailabilityState:
    def __init__(self, directory):
        import json

        self.path = directory / "key-availability.json"
        self.data = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def record(self, account, month, game, status, deadline="", time=None, source="operation"):
        if not account:
            return
        games = self.data.setdefault(account, {}).setdefault(month, {})
        if status is None:
            games.pop(game, None)
        else:
            old = games.get(game, {})
            games[game] = {"status": status, "reason": REASONS[status],
                           "deadline": deadline or old.get("deadline", ""),
                           "time": time or utc_now(), "source": source}
        atomic_json(self.path, self.data)

    def games(self, account, url):
        return self.data.get(account, {}).get(url.rstrip("/").rsplit("/", 1)[-1], {})

    def annotate(self, snapshot, account):
        snapshot = copy.deepcopy(snapshot)
        for month in snapshot["memberships"]:
            month["unavailable_games"] = self.games(account, month["url"])
            for row in snapshot["rows"]:
                if row.get("key") or row.get("bundle_name") != month["bundle_name"]:
                    continue
                mark = month["unavailable_games"].get(row["game_title"])
                if mark:
                    row["unavailable_status"] = mark["status"]
                    row["unavailable_reason"] = mark["reason"]
                    if mark["deadline"]:
                        row["redemption_deadline"] = mark["deadline"]
        annotate_rows(snapshot["rows"])
        for row in snapshot["rows"]:
            if row.get("unavailable_status") == "expired":
                row["deadline_state"] = "expired"
                row["has_deadline"] = True
            elif row["deadline_state"] == "expired" and not row.get("key"):
                row["unavailable_status"] = "expired"
                row["unavailable_reason"] = REASONS["expired"]
        return snapshot
