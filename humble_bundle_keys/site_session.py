"""Read-only helpers for Humble's Next.js storefront and new authentication."""
import html
import json
import re
from urllib.parse import urljoin, urlparse

LOGIN_URL = "https://auth.humblebundle.com/login"
LIBRARY_URL = "https://www.humblebundle.com/home/library"
SESSION_URL = "https://www.humblebundle.com/api/auth/session-status"


def _flight_values(value):
    if isinstance(value, dict):
        return {key: _flight_values(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_flight_values(child) for child in value]
    if value == "$undefined":
        return None
    if isinstance(value, str) and value.startswith("$D"):
        return value[2:]
    if isinstance(value, str) and value.startswith("$$"):
        return value[1:]
    return value


def flight_records(text):
    """Parse JSON records only; never evaluate scripts returned by the website."""
    if "__next_f.push(" in text:
        chunks = []
        for script in re.findall(r"<script\b[^>]*>(.*?)</script>", text, re.S):
            if "__next_f.push(" not in script:
                continue
            try:
                value = json.loads(script.split(".push(", 1)[1].rsplit(")", 1)[0])
                if value[0] == 1 and isinstance(value[1], str):
                    chunks.append(value[1])
            except (ValueError, TypeError, IndexError):
                continue
        text = "".join(chunks)
    records = []
    for line in text.splitlines():
        match = re.match(r"^[0-9a-f]+:(.*)$", line)
        if match:
            try:
                records.append(_flight_values(json.loads(match[1])))
            except ValueError:
                continue
    return records


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def account_email(document):
    addresses = {obj["email"].strip().lower() for obj in objects(flight_records(document))
                 if "emailVerified" in obj and "id" in obj
                 and isinstance(obj.get("email"), str)
                 and re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", obj["email"].strip())}
    if len(addresses) != 1:
        raise ValueError("新版 Humble 账号无法唯一识别，请重新登录。")
    return addresses.pop()


def session_status(context):
    """None denotes a legacy site, not an authenticated session."""
    response = context.request.get(SESSION_URL, timeout=15_000)
    try:
        if response.status == 404:
            return None
        if response.status != 200:
            return "unavailable"
        body = response.json()
        return body.get("status", "unavailable") if isinstance(body, dict) else "unavailable"
    finally:
        response.dispose()


def script_urls(document):
    result = []
    for src in re.findall(r'<script\b[^>]+src=["\']([^"\']+)', document):
        url = urljoin(LIBRARY_URL, html.unescape(src))
        parsed = urlparse(url)
        if (parsed.scheme == "https" and parsed.hostname == "www.humblebundle.com"
                and parsed.path.startswith("/_next/static/") and parsed.path.endswith(".js")):
            result.append(url)
    return list(dict.fromkeys(result))


def library_action(source):
    match = re.search(r'createServerReference\)\("([0-9a-f]{40,64})"[^;]{0,180}?'
                      r'"filterUserLibrary"\)', source)
    return match[1] if match else None


def redemption_action(source):
    match = re.search(r'createServerReference\)\("([0-9a-f]{40,64})"[^;]{0,180}?'
                      r'"redeemEntitlementAction"\)', source)
    return match[1] if match else None
