import asyncio
from datetime import datetime, timezone

import pytest

from humble_bundle_keys.api import ORDERS_LIST_URL
from humble_bundle_keys.async_inventory import ReadOnlyScanner
from humble_bundle_keys.deadlines import deadline_info
from humble_bundle_keys.web import describe_membership, is_legacy_monthly, membership_url


class Response:
    status = 200
    headers = {}

    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body

    async def dispose(self):
        pass


class Request:
    def __init__(self, count=12):
        self.active = self.maximum = self.detail_requests = 0
        self.count = count

    async def get(self, url, **kwargs):
        if url == ORDERS_LIST_URL:
            return Response([{"gamekey": f"order{i}"} for i in range(self.count)])
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.detail_requests += 1
        await asyncio.sleep(.01)
        self.active -= 1
        name = url.split("/")[-1].split("?")[0]
        # One unresolved order, all the other orders fully revealed.
        return Response({"gamekey": name, "product": {
            "human_name": "April 2018 Humble Monthly",
            "category": "subscriptioncontent", "machine_name": "april_2018_monthly"},
            "tpkd_dict": {"all_tpks": [{"human_name": name, "key_type": "steam",
                                        "redeemed_key_val": "" if name == "order0"
                                        else "TEST-ONLY-KEY"}]}})

    async def post(self, *args, **kwargs):
        raise AssertionError("A read-only scanner must never post")


def test_concurrency_cache_legacy_monthly_and_checkpoints(tmp_path):
    request = Request()

    class Context:
        async def new_page(self):
            raise AssertionError("Legacy Monthly must not open a Choice page")

    context = Context()
    context.request = request
    snapshots, events = [], []

    def make(full=False):
        return ReadOnlyScanner(context, tmp_path, snapshots.append, lambda message: None,
                               describe_membership, membership_url, full=full,
                               legacy=is_legacy_monthly,
                               log=lambda event, **fields: events.append(event))

    asyncio.run(make().run())
    assert request.maximum == 3
    assert len(snapshots[-1]["rows"]) == 12
    assert snapshots[-1]["memberships"] == []
    assert snapshots[-1]["warnings"] == []
    assert "legacy_monthly_skipped" in events
    assert any(s["partial"] for s in snapshots[:-1])
    assert not snapshots[-1]["partial"]
    request.detail_requests = 0
    asyncio.run(make().run())
    assert request.detail_requests == 1  # unresolved order refreshed; others cached
    request.detail_requests = 0
    asyncio.run(make(full=True).run())
    assert request.detail_requests == 12


def test_retry_disposes_responses_and_recovers(tmp_path, monkeypatch):
    disposed = []

    class RateLimited(Response):
        status = 429

        async def dispose(self):
            disposed.append(True)

    class RetryRequest:
        calls = 0

        async def get(self, url, **kwargs):
            self.calls += 1
            return RateLimited([]) if self.calls == 1 else Response([])

    class Context:
        request = RetryRequest()

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    scanner = ReadOnlyScanner(Context(), tmp_path, lambda data: None, lambda text: None,
                              describe_membership, membership_url)
    assert asyncio.run(scanner.get_json(ORDERS_LIST_URL)) == []
    assert disposed == [True]
    assert scanner.context.request.calls == 2


def test_month_failure_has_http_diagnostic_without_private_urls(tmp_path):
    url = "https://www.humblebundle.com/membership/january-2020"
    events, snapshots = [], []

    class Page:
        async def goto(self, requested, **kwargs):
            self.url = requested
            response = Response(None)
            response.status = 403
            return response

        async def close(self):
            pass

        def locator(self, selector):
            raise AssertionError("HTTP errors must skip card lookup")

    class Context:
        async def new_page(self):
            return Page()

    scanner = ReadOnlyScanner(Context(), tmp_path, snapshots.append, lambda text: None,
                              describe_membership, membership_url,
                              log=lambda event, **fields: events.append((event, fields)))
    asyncio.run(scanner.scan_month({"product": {"human_name": "Test Choice"}}, url, 0, 1))
    failure = next(fields for event, fields in events if event == "month_failed")
    assert failure["reason"] == "http_error"
    assert failure["http_status"] == 403
    assert "url" not in failure
    assert scanner.months[url]["state"] == "unknown"


@pytest.mark.parametrize("text,expected", [
    ("", "unknown"), ("Expired", "expired"),
    ("Must be redeemed by 2025-01-01T19:00:00", "expired"),
    ("Must be redeemed by 2027-02-04T19:00:00", "not_expired"),
    ("Must be redeemed by January 6th, 2027.", "not_expired"),
    ("Must be redeemed by February 30th, 2027.", "unknown"),
    ("Must be redeemed by 2026-10-03", "not_expired"),
    ("2026-10-03T10:00:00+02:00", "expired"),
    ("Ask the publisher", "unknown"),
])
def test_deadline_filter(text, expected):
    info = deadline_info(text, datetime(2026, 10, 3, 12, tzinfo=timezone.utc))
    assert info["deadline_state"] == expected
    assert info["has_deadline"] == bool(text)
