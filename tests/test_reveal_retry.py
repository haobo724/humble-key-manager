"""Tests for the reveal retry logic (transient Cloudflare 403 handling).

Covers bugs #1/#2: a single transient 403 during a reveal POST should be
retried with backoff rather than immediately recorded as a failure.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from humble_bundle_keys._browser_fetch import BrowserFetchResponse
from humble_bundle_keys.api import ApiOptions, ApiScraper
from humble_bundle_keys.models import ExtractStats


def _make_scraper() -> ApiScraper:
    """Create an ApiScraper with a mocked BrowserContext."""
    ctx = MagicMock()
    ctx.cookies.return_value = []
    opts = ApiOptions(reveal_keys=True, dry_run=False, polite_delay_ms=0)
    scraper = ApiScraper(ctx, opts)
    scraper.stats = ExtractStats()
    # Pre-set anchor page so _get_anchor_page doesn't try to open a real browser
    scraper._anchor_page = MagicMock()
    scraper._anchor_page.is_closed.return_value = False
    scraper._anchor_page.url = "https://www.humblebundle.com/home/keys"
    return scraper


SAMPLE_TPK = {
    "machine_name": "risingstorm2_bundle_steam",
    "human_name": "Rising Storm 2: Vietnam + 2 DLCs",
    "keyindex": 0,
}

SAMPLE_ORDER = {"gamekey": "abc123"}


@patch("humble_bundle_keys.api.post_form_in_browser")
@patch("humble_bundle_keys.api.time.sleep")
def test_reveal_retries_on_transient_403(mock_sleep, mock_post) -> None:
    """A single 403 followed by a 200 should succeed without recording an error."""
    mock_post.side_effect = [
        BrowserFetchResponse(status=403, body=None, raw_text="<html>cloudflare</html>"),
        BrowserFetchResponse(
            status=200,
            body={"key": "AAAAA-BBBBB-CCCCC"},
            raw_text='{"key":"AAAAA-BBBBB-CCCCC"}',
        ),
    ]
    scraper = _make_scraper()
    result = scraper._reveal(SAMPLE_TPK, SAMPLE_ORDER)
    assert result == "AAAAA-BBBBB-CCCCC"
    assert len(scraper.stats.errors) == 0
    # Should have slept between retries
    assert mock_sleep.call_count >= 1


@patch("humble_bundle_keys.api.post_form_in_browser")
@patch("humble_bundle_keys.api.time.sleep")
def test_reveal_fails_after_max_retries(mock_sleep, mock_post) -> None:
    """Three consecutive 403s should record an error after exhausting retries."""
    mock_post.return_value = BrowserFetchResponse(
        status=403, body=None, raw_text="<html>cloudflare</html>"
    )
    scraper = _make_scraper()
    result = scraper._reveal(SAMPLE_TPK, SAMPLE_ORDER)
    assert result is None
    assert len(scraper.stats.errors) == 1
    assert "403" in scraper.stats.errors[0]
    # Should have been called 3 times (initial + 2 retries)
    assert mock_post.call_count == 3


@patch("humble_bundle_keys.api.post_form_in_browser")
@patch("humble_bundle_keys.api.time.sleep")
def test_reveal_no_retry_on_non_403_error(mock_sleep, mock_post) -> None:
    """A 400 or 500 should fail immediately without retrying."""
    mock_post.return_value = BrowserFetchResponse(
        status=400, body=None, raw_text="bad request"
    )
    scraper = _make_scraper()
    result = scraper._reveal(SAMPLE_TPK, SAMPLE_ORDER)
    assert result is None
    assert len(scraper.stats.errors) == 1
    # Only called once — no retry for non-403
    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


@patch("humble_bundle_keys.api.post_form_in_browser")
@patch("humble_bundle_keys.api.time.sleep")
def test_reveal_retries_on_exception(mock_sleep, mock_post) -> None:
    """Network exceptions should also be retried."""
    mock_post.side_effect = [
        Exception("Connection reset"),
        BrowserFetchResponse(
            status=200,
            body={"key": "XXXXX-YYYYY-ZZZZZ"},
            raw_text='{"key":"XXXXX-YYYYY-ZZZZZ"}',
        ),
    ]
    scraper = _make_scraper()
    result = scraper._reveal(SAMPLE_TPK, SAMPLE_ORDER)
    assert result == "XXXXX-YYYYY-ZZZZZ"
    assert len(scraper.stats.errors) == 0
