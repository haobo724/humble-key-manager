"""Tests for the browser-fetch helper.

The helper itself is a thin wrapper around ``page.evaluate(...)``. Real
verification needs a live Playwright page, which we don't run in CI. So
we test the deterministic parts: the JS snippet shape and the call
signature passed into evaluate().
"""

from __future__ import annotations

from unittest.mock import MagicMock

from humble_bundle_keys._browser_fetch import (
    FETCH_SCRIPT,
    BrowserFetchResponse,
    post_form_in_browser,
)


def test_fetch_script_uses_same_origin_credentials() -> None:
    """JS snippet does NOT use credentials:'include' — same-origin sends cookies automatically."""
    # Ignore the comment line that explains the absence; check actual code shape.
    assert "credentials:" not in FETCH_SCRIPT
    assert "credentials :" not in FETCH_SCRIPT


def test_fetch_script_returns_status_body_raw_text() -> None:
    """The shape we depend on in BrowserFetchResponse."""
    assert "status: resp.status" in FETCH_SCRIPT
    assert "raw_text: text" in FETCH_SCRIPT
    assert "body: parsed" in FETCH_SCRIPT


def test_fetch_script_handles_json_parse_failure() -> None:
    """If response isn't JSON, we still get raw_text and body=null (not crash)."""
    assert "JSON.parse(text)" in FETCH_SCRIPT
    assert "catch" in FETCH_SCRIPT


def test_post_form_in_browser_calls_evaluate_with_post() -> None:
    page = MagicMock()
    page.evaluate.return_value = {
        "status": 200,
        "raw_text": '{"key":"X-Y-Z","success":true}',
        "body": {"key": "X-Y-Z", "success": True},
    }
    resp = post_form_in_browser(
        page,
        "https://www.humblebundle.com/humbler/redeemkey",
        "keytype=foo&key=bar&keyindex=0",
        referer="https://www.humblebundle.com/home/keys",
    )
    assert isinstance(resp, BrowserFetchResponse)
    assert resp.status == 200
    assert resp.body == {"key": "X-Y-Z", "success": True}
    # Verify it sent the right shape to page.evaluate
    page.evaluate.assert_called_once()
    call = page.evaluate.call_args
    payload = call.args[1]
    assert payload["url"] == "https://www.humblebundle.com/humbler/redeemkey"
    assert payload["method"] == "POST"
    assert payload["body"] == "keytype=foo&key=bar&keyindex=0"
    headers = payload["headers"]
    assert headers["content-type"] == "application/x-www-form-urlencoded; charset=UTF-8"
    assert headers["x-requested-with"] == "XMLHttpRequest"
    assert "json" in headers["accept"].lower()
    assert headers["referer"] == "https://www.humblebundle.com/home/keys"


def test_post_form_in_browser_merges_extra_headers() -> None:
    page = MagicMock()
    page.evaluate.return_value = {"status": 200, "raw_text": "{}", "body": {}}
    post_form_in_browser(
        page,
        "https://www.humblebundle.com/humbler/redeemkey",
        "keytype=foo",
        extra_headers={"CSRF-Prevention-Token": "abc123"},
    )
    headers = page.evaluate.call_args.args[1]["headers"]
    assert headers["CSRF-Prevention-Token"] == "abc123"
    # Standard headers still present
    assert headers["x-requested-with"] == "XMLHttpRequest"


def test_post_form_in_browser_handles_403_response() -> None:
    """When Cloudflare or Humble returns 4xx, we surface status + raw_text."""
    page = MagicMock()
    page.evaluate.return_value = {
        "status": 403,
        "raw_text": "<!DOCTYPE html><html>cloudflare blocked</html>",
        "body": None,  # not JSON
    }
    resp = post_form_in_browser(
        page,
        "https://www.humblebundle.com/humbler/redeemkey",
        "keytype=foo",
    )
    assert resp.status == 403
    assert resp.body is None
    assert "cloudflare" in resp.raw_text.lower()
