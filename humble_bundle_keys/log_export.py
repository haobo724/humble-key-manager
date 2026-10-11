"""Shareable diagnostics: allowlisted events, no account files or raw payloads."""
import json
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from humble_bundle_keys import __version__

FIELDS = {
    "time", "event", "action", "operation_id", "phase", "bundle", "month", "game",
    "order_ref", "order_id", "error", "error_type", "reason", "source", "http_status",
    "redirected", "claimed", "key_exhausted", "count", "rows", "months", "warnings",
    "workers", "cards", "state", "succeeded", "failed", "skipped", "full_refresh",
}
SECRET_FIELDS = {
    "key", "redeemed_key_val", "key_val", "api_key", "client_secret", "client_id",
    "password", "email", "steamid", "gamekey", "identity", "current_account",
    "access_token", "refresh_token", "authorization",
}


def known_secrets(directory):
    secrets = set()

    def collect(value):
        if isinstance(value, dict):
            for name, child in value.items():
                if name == "account" and isinstance(child, dict):
                    label = child.get("name")
                    if isinstance(label, str) and len(label) >= 3:
                        secrets.add(label)
                if name.lower() in SECRET_FIELDS and isinstance(child, str) and len(child) >= 6:
                    secrets.add(child)
                if name == "cookies" and isinstance(child, list):
                    for cookie in child:
                        token = cookie.get("value") if isinstance(cookie, dict) else None
                        if isinstance(token, str) and len(token) >= 6:
                            secrets.add(token)
                if name == "humble_url" and isinstance(child, str):
                    for tokens in parse_qs(urlparse(child).query).values():
                        secrets.update(t for t in tokens if len(t) >= 6)
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    # Read only to build a replacement list. These files never enter the export.
    for name in ("inventory.json", "storage_state.json", "steam-storage-state.json",
                 "itad-settings.json", "hb-activations.json", "steam.json"):
        try:
            collect(json.loads((directory / name).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(secrets, key=len, reverse=True)


def redact(text, secrets=()):
    text = str(text)
    for secret in secrets:
        text = text.replace(secret, "[凭据已隐藏]")
    text = re.sub(r"https?://[^\s\"'<>]+", "[网址已隐藏]", text, flags=re.I)
    text = re.sub(r"[A-Z]:[\\/][^\r\n\"'<>]+", "[本机路径已隐藏]", text, flags=re.I)
    text = re.sub(r"/(?:Users|home|tmp|var|mnt|opt)/[^\s\"'<>]+",
                  "[本机路径已隐藏]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已隐藏]", text)
    text = re.sub(r"\b[A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8}){1,4}\b", "[Key 已隐藏]", text)
    text = re.sub(r"\b[a-fA-F0-9]{24,}\b|\b\d{17}\b", "[账号或凭据已隐藏]", text)
    text = re.sub(r"(?i)\b(?:cookie|authorization)\s*[:=][^\r\n]+",
                  "[认证信息已隐藏]", text)
    text = re.sub(r"(?i)\b(?:api[_-]?key|token|client_secret|gamekey|password)"
                  r"[\"']?\s*[:=]\s*[\"']?[^\s,;\"'}]+", "[凭据已隐藏]", text)
    return text[:4000]


def export_logs(directory, entries=()):
    secrets = known_secrets(directory)
    events, malformed = [], 0
    found = False
    for name in ("scan.log.2", "scan.log.1", "scan.log"):
        try:
            with (directory / name).open(encoding="utf-8") as file:
                found = True
                for line in file:
                    try:
                        entry = json.loads(line)
                        if not isinstance(entry, dict):
                            raise ValueError("Invalid log entry")
                    except ValueError:
                        malformed += 1
                        continue
                    events.append(entry)
        except OSError:
            continue
    if not found:
        events = list(entries)
    safe = []
    for entry in events[-10000:]:
        item = {name: redact(value, secrets) if isinstance(value, str) else value
                for name, value in entry.items() if name in FIELDS
                and (value is None or isinstance(value, (str, int, float, bool)))}
        if isinstance(entry.get("locations"), list):
            item["locations"] = [
                {name: redact(Path(value.replace("\\", "/")).name, secrets)
                 if name == "file" else redact(value, secrets) if isinstance(value, str) else value
                 for name, value in frame.items() if name in {"file", "function", "line"}
                 and isinstance(value, (str, int))}
                for frame in entry["locations"] if isinstance(frame, dict)]
        safe.append(item)
    return {"format": "humble-key-manager-debug-v1", "version": __version__,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "system": platform.system(), "events": safe, "malformed_lines_skipped": malformed}
