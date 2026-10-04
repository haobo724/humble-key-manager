"""Read Humble's per-key country metadata; never infer restrictions from an AppID."""
import json
import re
from dataclasses import asdict


def countries(value):
    if not isinstance(value, list):
        return None
    if any(not isinstance(c, str) or not re.fullmatch(r"[A-Za-z]{2}", c) for c in value):
        return None
    return sorted({c.upper() for c in value})


def annotate_regions(rows):
    for row in rows:
        allow = countries(row.get("exclusive_countries"))
        deny = countries(row.get("disallowed_countries"))
        row["china_activation"] = "unknown"
        if row.get("platform") != "steam":
            row["china_activation"] = "not_applicable"
        elif (deny is not None and "CN" in deny) or (allow and "CN" not in allow):
            row["china_activation"] = "blocked"
        elif allow is not None and deny is not None:
            row["china_activation"] = "allowed" if allow or deny else "unrestricted"
        row["region_details"] = ("仅限：" + ", ".join(allow) if allow else "")
        if deny:
            separator = "；" if row["region_details"] else ""
            row["region_details"] += separator + "禁止：" + ", ".join(deny)
    return rows


def hydrate_restrictions(rows, directory):
    """Upgrade saved records using exact order/title/platform matches in the local cache."""
    from humble_bundle_keys.api import _extract_tpk
    from humble_bundle_keys.async_inventory import order_tpks

    mapping = {}
    for path in (directory / "orders-cache").glob("*.json"):
        try:
            order = json.loads(path.read_text(encoding="utf-8"))
            for tpk in order_tpks(order):
                game = asdict(_extract_tpk(tpk, order))
                identity = tuple(game[k] for k in ("humble_url", "game_title", "platform"))
                value = (game["exclusive_countries"], game["disallowed_countries"])
                mapping.setdefault(identity, []).append((game["key"], value))
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    changed = False
    for row in rows:
        if (row.get("exclusive_countries") is not None
                and row.get("disallowed_countries") is not None):
            continue
        identity = tuple(row.get(k, "") for k in ("humble_url", "game_title", "platform"))
        entries = mapping.get(identity, [])
        values = [value for key, value in entries if not row.get("key") or key in {"", row["key"]}]
        if values and all(value == values[0] for value in values):
            allow, deny = values[0]
            if allow is not None or deny is not None:
                if row.get("exclusive_countries") is None:
                    row["exclusive_countries"] = allow
                if row.get("disallowed_countries") is None:
                    row["disallowed_countries"] = deny
                changed = True
    return changed
