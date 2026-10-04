"""Account-scoped local labels; no plaintext activation keys are stored here."""
import json

from humble_bundle_keys.steam import atomic_json, key_digest, row_id

SOLD_TAGS = {"卖掉了", "已出售", "已卖出", "已赠送"}
EXCLUDED_TAGS = SOLD_TAGS | {"已激活"}


def identities(row):
    record = "record:" + row_id({**row, "key": ""})
    return ("key:" + key_digest(row["key"]) if row.get("key") else record), record


class KeyTags:
    def __init__(self, directory):
        self.path = directory / "key-tags.json"
        self.data = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def set(self, account, rows, tags, excluded):
        if not account:
            raise ValueError("请先登录或扫描 Humble，识别标签所属账号。")
        if (not isinstance(tags, list) or len(tags) > 20
                or any(not isinstance(t, str) or not t.strip() or len(t) > 40 for t in tags)
                or type(excluded) is not bool):
            raise ValueError("最多 20 个标签，每个 1–40 字。")
        labels = list(dict.fromkeys(t.strip() for t in tags))
        marks = self.data.setdefault(account, {})
        for row in rows:
            identity, record = identities(row)
            marks[identity] = {"tags": labels, "exclude_activation": excluded}
            if identity != record:
                marks.pop(record, None)
        atomic_json(self.path, self.data)

    def annotate(self, rows, account):
        marks = self.data.get(account, {})
        result = []
        for original in rows:
            row = original.copy()
            identity, record = identities(row)
            mark = marks.get(identity, marks.get(record, {}))
            row["custom_tags"] = list(mark.get("tags", []))
            row["exclude_activation"] = bool(mark.get("exclude_activation"))
            row["manual_activated"] = "已激活" in row["custom_tags"]
            row["activation_excluded"] = (row["exclude_activation"]
                                          or bool(EXCLUDED_TAGS.intersection(row["custom_tags"])))
            result.append(row)
        return result
