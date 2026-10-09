import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier
from unittest.mock import MagicMock

import pytest

from humble_bundle_keys.api import ApiOptions, ApiScraper, ApiUnsupported
from humble_bundle_keys.diagnostics import error_fields, fields, operation, scope, set_context
from humble_bundle_keys.web import Inventory


def test_order_parse_failure_identifies_game_without_payload(tmp_path):
    inventory = Inventory(tmp_path)
    scraper = ApiScraper(MagicMock(), ApiOptions(reveal_keys=False, dry_run=True))
    scraper._list_gamekeys = MagicMock(return_value=["private-order-token"])
    scraper._get_order = MagicMock(return_value={
        "gamekey": "private-order-token", "product": {"human_name": "April Fixture Choice"},
        "tpkd_dict": {"all_tpks": [{"human_name": "Fixture Game", "key_type": "steam",
                                   "redeemed_key_val": {"opaque": "private-payload"}}]}})

    class Job:
        log = inventory.log

        @operation
        def run(self, action):
            scraper.scrape()

    Job().run("reveal")
    entry = inventory.logs[-1]
    assert entry["event"] == "order_parse_failed"
    assert entry["bundle"] == "April Fixture Choice"
    assert entry["game"] == "Fixture Game"
    assert entry["phase"] == "解析订单 Key"
    assert entry["action"] == "reveal"
    assert entry["operation_id"] and entry["locations"]
    text = (tmp_path / "scan.log").read_text(encoding="utf-8")
    assert "private-order-token" not in text
    assert "private-payload" not in text


def test_publish_error_keeps_exact_row_context(tmp_path):
    inventory = Inventory(tmp_path)
    with scope(action="reveal", bundle="Other month", game="Other game"):
        with pytest.raises(ApiUnsupported) as caught:
            inventory.publish({"rows": [{"bundle_name": "Fixture Bundle",
                                         "game_title": "Broken Game", "key": {"opaque": "secret"}}],
                               "memberships": []})
        diagnostic = error_fields(caught.value)
        assert diagnostic["bundle"] == "Fixture Bundle"
        assert diagnostic["game"] == "Broken Game"
        assert diagnostic["phase"] == "保存库存 Key"
        assert fields()["game"] == "Other game"
        assert "secret" not in json.dumps(diagnostic)


def test_parallel_failure_context_survives_outer_scope():
    barrier = Barrier(2)

    def worker(name):
        try:
            with scope(bundle=name, game="", phase="打开月包"):
                set_context(game=name + " Game", phase="刮取 Key")
                barrier.wait(timeout=5)
                raise TypeError("unhashable type: 'dict'")
        except TypeError as exc:
            return error_fields(exc)

    with scope(action="claim-months", operation_id="fixture-operation"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(copy_context().run, worker, name) for name in ("April", "May")]
            results = [future.result(timeout=5) for future in futures]
        for name, result in zip(("April", "May"), results, strict=True):
            assert result["bundle"] == name
            assert result["game"] == name + " Game"
            assert result["operation_id"] == "fixture-operation"
        assert fields().get("game") is None


def test_top_level_error_has_captured_context(tmp_path, monkeypatch):
    from humble_bundle_keys import web

    def fail(*args, **kwargs):
        with scope(bundle="April Fixture Choice", game="Broken Game", phase="API 领取并刮取"):
            raise TypeError("unhashable type: 'dict'")

    monkeypatch.setattr(web, "sync_playwright", MagicMock())
    monkeypatch.setattr(web, "get_authenticated_context", fail)
    inventory = Inventory(tmp_path)
    inventory.run("reveal")
    entry = inventory.logs[-1]
    assert entry["event"] == "operation_failed"
    assert entry["bundle"] == "April Fixture Choice"
    assert entry["game"] == "Broken Game"
    assert entry["error_type"] == "TypeError"
    assert entry["action"] == "reveal"
    assert entry["locations"][-1]["function"] == "fail"
