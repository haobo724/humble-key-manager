"""Preview account-local label updates from exact-key activation receipts."""
import hashlib
import json

from humble_bundle_keys.key_tags import identities
from humble_bundle_keys.steam import key_digest, row_id


def candidates(rows, account, tags, hb, steam):
    if not account:
        return []
    marks = tags.data.get(account, {})
    successes = hb.data.get("accounts", {}).get(account, {})
    updates = []
    for row in rows:
        if not row.get("key"):
            continue
        identity, record = identities(row)
        mark = marks.get(identity, marks.get(record, {}))
        if "已激活" in mark.get("tags", []) and mark.get("activation_source"):
            continue
        digest = key_digest(row["key"])
        source = None
        if row.get("platform") == "steam":
            if digest in successes:
                source = "system"
            for receipts in steam.data.get("results", {}).values():
                receipt = receipts.get(digest, {})
                if receipt.get("humble_account") not in {None, account}:
                    continue
                if receipt.get("status") == "activated":
                    source = "system"
                elif receipt.get("status") == "duplicate" and source != "system":
                    source = "duplicate"
        if not source and "已激活" in mark.get("tags", []):
            source = "manual"
        if source:
            updates.append((row, source))
    return updates


def preview(updates, account, revision):
    if not updates:
        return None
    signature = [account, sorted((row_id(row), source) for row, source in updates)]
    identity = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
    names = {"system": "本系统激活成功记录", "duplicate": "Steam 返回 Key 已使用",
             "manual": "旧版手动已激活标签"}
    return {"plan_id": identity, "revision": revision, "count": len(updates),
            "counts": {source: sum(s == source for _, s in updates) for source in names},
            "games": [{"game": row["game_title"], "bundle": row.get("bundle_name", ""),
                       "reason": names[source]} for row, source in updates]}
