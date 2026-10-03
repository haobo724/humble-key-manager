"""Durable, key-free progress and outcome summaries for revelation operations."""
import hashlib
import json
import secrets
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def work_id(source, title, platform=""):
    return hashlib.sha256(json.dumps([source, title, platform]).encode()).hexdigest()[:24]


class OperationReport:
    def __init__(self, action, estimated=False):
        self.data = {"id": secrets.token_hex(12), "action": action, "status": "running",
                     "started_at": now(), "ended_at": None, "phase": "准备与登录",
                     "current_game": "", "current_bundle": "", "estimated": estimated,
                     "issues": [], "items": {}}

    @classmethod
    def load(cls, path):
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data.get("items"), dict):
                return None
            report = cls(data["action"])
            report.data = data
            if data["status"] == "running":
                report.finish(interrupted=True)
                report.save(path)
            return report
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def add(self, identity, title, bundle=""):
        self.data["items"].setdefault(identity, {"game": title, "bundle": bundle,
                                              "status": "pending", "reason": ""})

    def result(self, identity, status, reason=""):
        item = self.data["items"][identity]
        # A later refresh failure must never erase a successfully saved key.
        if item["status"] == "success" and status != "success":
            return
        item.update(status=status, reason=reason)

    def issue(self, reason):
        if reason not in self.data["issues"]:
            self.data["issues"].append(reason)

    def finish(self, stopped=False, error=None, interrupted=False):
        if self.data["status"] != "running":
            return
        if error:
            self.issue(error)
        view = self.view()
        if interrupted:
            status = "interrupted"
        elif stopped:
            status = "stopped"
        elif view["failed"] or view["remaining"] or self.data["issues"]:
            status = "partial" if view["succeeded"] else "failed"
        else:
            status = "success"
        self.data.update(status=status, ended_at=now(), phase="已结束", current_game="",
                         current_bundle="")

    def view(self):
        items = list(self.data["items"].values())
        counts = {status: sum(i["status"] == status for i in items)
                  for status in ("success", "failed", "skipped", "pending")}
        end = self.data["ended_at"] or now()
        elapsed = max(0, int((datetime.fromisoformat(end) -
                              datetime.fromisoformat(self.data["started_at"])).total_seconds()))
        return {**self.data, "items": items, "total": len(items),
                "processed": len(items) - counts["pending"], "succeeded": counts["success"],
                "failed": counts["failed"], "skipped": counts["skipped"],
                "remaining": counts["pending"], "elapsed_seconds": elapsed}

    def save(self, path):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
