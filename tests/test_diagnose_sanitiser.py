"""Tests for the diagnose sanitiser.

These tests are critical: if the sanitiser regresses, real keys could leak
in artifacts users share publicly.
"""

from __future__ import annotations

from humble_bundle_keys.diagnose import (
    EMAIL_PATTERN,
    GAMEKEY_URL_PARAM,
    KEY_PATTERN,
    _is_static_asset,
    _redact_headers,
    _sanitise_json,
    _sanitise_text,
)

# ---------------------------------------------------------------------------
# Regex sanity
# ---------------------------------------------------------------------------

def test_key_pattern_matches_steam_shaped() -> None:
    assert KEY_PATTERN.search("AAAAA-BBBBB-CCCCC") is not None


def test_email_pattern_matches() -> None:
    assert EMAIL_PATTERN.search("contact me at user@example.com please") is not None


def test_gamekey_url_param_matches() -> None:
    assert GAMEKEY_URL_PARAM.search("?gamekey=ABC123def456&foo=bar") is not None


# ---------------------------------------------------------------------------
# _sanitise_text
# ---------------------------------------------------------------------------

def test_sanitise_text_redacts_steam_key() -> None:
    out = _sanitise_text("Your key is AAAAA-BBBBB-CCCCC, please redeem.")
    assert "AAAAA-BBBBB-CCCCC" not in out
    assert "REDACTED-KEY" in out


def test_sanitise_text_redacts_email() -> None:
    out = _sanitise_text("Sent to testuser@example.com on Tuesday")
    assert "testuser@example.com" not in out
    assert "REDACTED@example.com" in out


def test_sanitise_text_redacts_gamekey_url() -> None:
    out = _sanitise_text(
        "https://www.humblebundle.com/downloads?gamekey=AbCdEf012345&foo=bar"
    )
    assert "AbCdEf012345" not in out
    assert "REDACTED-GAMEKEY" in out
    # Other query params left alone
    assert "foo=bar" in out


def test_sanitise_text_preserves_innocent_strings() -> None:
    s = "Nine Sols was published in 2024 by Red Candle Games."
    out = _sanitise_text(s)
    assert out == s


# ---------------------------------------------------------------------------
# _sanitise_json
# ---------------------------------------------------------------------------

def test_sanitise_json_redacts_redeemed_key_val() -> None:
    obj = {
        "human_name": "Nine Sols",
        "redeemed_key_val": "AAAAA-BBBBB-CCCCC",
    }
    out = _sanitise_json(obj)
    assert out["redeemed_key_val"] == "REDACTED-KEY"
    assert out["human_name"] == "Nine Sols"  # public, untouched


def test_sanitise_json_preserves_null_keys() -> None:
    obj = {"redeemed_key_val": None}
    out = _sanitise_json(obj)
    assert out["redeemed_key_val"] is None


def test_sanitise_json_redacts_email_field() -> None:
    obj = {"customer_email": "user@example.com", "human_name": "Stray"}
    out = _sanitise_json(obj)
    assert out["customer_email"] == "REDACTED"
    assert out["human_name"] == "Stray"


def test_sanitise_json_recurses_into_arrays() -> None:
    obj = {
        "tpkd_dict": {
            "all_tpks": [
                {"human_name": "A", "redeemed_key_val": "AAAA-BBBB-CCCC"},
                {"human_name": "B", "redeemed_key_val": None},
            ]
        }
    }
    out = _sanitise_json(obj)
    tpks = out["tpkd_dict"]["all_tpks"]
    assert tpks[0]["redeemed_key_val"] == "REDACTED-KEY"
    assert tpks[1]["redeemed_key_val"] is None
    assert tpks[0]["human_name"] == "A"


def test_sanitise_json_redacts_inline_keys_in_string_fields() -> None:
    """If a key shows up inline in a non-sensitive field, sanitise it too."""
    obj = {"instructions_html": "Use code AAAAA-BBBBB-CCCCC on Steam."}
    out = _sanitise_json(obj)
    assert "AAAAA-BBBBB-CCCCC" not in out["instructions_html"]
    assert "REDACTED-KEY" in out["instructions_html"]


def test_sanitise_json_preserves_structure() -> None:
    obj = {
        "outer": {
            "inner": [1, 2, {"deep": "value"}],
            "n": 42,
            "flag": True,
        }
    }
    out = _sanitise_json(obj)
    assert out == obj  # nothing sensitive, must be preserved


# ---------------------------------------------------------------------------
# Integration: a realistic blob of mixed PII
# ---------------------------------------------------------------------------

REALISTIC_HTML_SNIPPET = """
<html><body>
  <div class="key">AAAAA-BBBBB-CCCCC</div>
  <a href="/downloads?gamekey=AbCd1234EfGh5678">View bundle</a>
  <span>From: testuser@example.com</span>
  <p>Title: Nine Sols (December 2025 Humble Choice)</p>
</body></html>
"""


def test_realistic_html_loses_all_sensitive_strings() -> None:
    out = _sanitise_text(REALISTIC_HTML_SNIPPET)
    # All three forms of PII gone
    assert "AAAAA-BBBBB-CCCCC" not in out
    assert "AbCd1234EfGh5678" not in out
    assert "testuser@example.com" not in out
    # All three replacements present
    assert "REDACTED-KEY" in out
    assert "REDACTED-GAMEKEY" in out
    assert "REDACTED@example.com" in out
    # Bundle / game names untouched (these are public)
    assert "Nine Sols" in out
    assert "December 2025 Humble Choice" in out


# ---------------------------------------------------------------------------
# _redact_headers — auth secrets must NEVER hit disk
# ---------------------------------------------------------------------------

def test_redact_headers_strips_cookie() -> None:
    headers = {"Cookie": "_simpleauth_sess=secrettokenvalue", "User-Agent": "Mozilla"}
    out = _redact_headers(headers)
    assert "Cookie" not in out
    assert out["User-Agent"] == "Mozilla"


def test_redact_headers_strips_authorization_case_insensitive() -> None:
    headers = {"AuThOrIzAtIoN": "Bearer abc123", "X-Other": "ok"}
    out = _redact_headers(headers)
    assert "AuThOrIzAtIoN" not in out
    assert out["X-Other"] == "ok"


def test_redact_headers_strips_csrf_variants() -> None:
    for name in ("CSRF-Prevention-Token", "X-CSRF-Token", "X-XSRF-Token"):
        headers = {name: "tokenvalue", "Accept": "application/json"}
        out = _redact_headers(headers)
        assert name not in out
        assert out["Accept"] == "application/json"


def test_redact_headers_strips_set_cookie() -> None:
    headers = {"Set-Cookie": "_simpleauth_sess=newvalue; Path=/"}
    out = _redact_headers(headers)
    assert out == {}


def test_redact_headers_handles_none_and_empty() -> None:
    assert _redact_headers(None) == {}
    assert _redact_headers({}) == {}


def test_redact_headers_preserves_safe_headers() -> None:
    headers = {
        "Content-Type": "application/json",
        "Accept-Language": "en-US",
        "Referer": "https://www.humblebundle.com/membership/march-2026",
    }
    out = _redact_headers(headers)
    assert out == headers


# ---------------------------------------------------------------------------
# _is_static_asset — we don't want CSS/JS/images bloating the bundle
# ---------------------------------------------------------------------------

def test_is_static_asset_recognises_extensions() -> None:
    for url in [
        "https://www.humblebundle.com/static/main.js",
        "https://www.humblebundle.com/static/main.css",
        "https://www.humblebundle.com/static/img/logo.png",
        "https://www.humblebundle.com/static/font.woff2",
        "https://www.humblebundle.com/img/hero.jpg?v=123",  # query string ignored
        "https://www.humblebundle.com/promo.mp4#t=5",  # fragment ignored
    ]:
        assert _is_static_asset(url), url


def test_is_static_asset_passes_api_urls() -> None:
    for url in [
        "https://www.humblebundle.com/api/v1/user/order",
        "https://www.humblebundle.com/api/v1/order/abc123?all_tpkds=true",
        "https://www.humblebundle.com/humbler/redeemkey",
        "https://www.humblebundle.com/membership/march-2026",
    ]:
        assert not _is_static_asset(url), url
