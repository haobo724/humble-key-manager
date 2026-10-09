import copy
from dataclasses import asdict
from unittest.mock import MagicMock

import pytest

from humble_bundle_keys.api import ApiOptions, ApiScraper, ApiUnsupported, _extract_tpk
from humble_bundle_keys.web import Inventory


def order(value):
    return {"gamekey": "fixture", "product": {"human_name": "Fixture"},
            "tpkd_dict": {"all_tpks": [{"human_name": "Fixture Game", "key_type": "steam",
                                       "redeemed_key_val": value}]}}


def test_wrapped_key_survives_publish_and_preserves_reveal_time(tmp_path):
    inventory = Inventory(tmp_path)
    data = order("AAAAA-BBBBB-CCCCC")
    row = asdict(_extract_tpk(data["tpkd_dict"]["all_tpks"][0], data))
    row["revealed_at"] = "2026-10-01T00:00:00Z"
    snapshot = {"rows": [row], "memberships": [], "warnings": [], "scanned_at": None}
    inventory.publish(snapshot)
    fresh = copy.deepcopy(snapshot)
    fresh["rows"][0]["key"] = {"key": "AAAAA-BBBBB-CCCCC"}
    fresh["rows"][0].pop("revealed_at")
    inventory.publish(fresh)
    assert inventory.snapshot["rows"][0]["key"] == "AAAAA-BBBBB-CCCCC"
    assert inventory.snapshot["rows"][0]["revealed_at"] == row["revealed_at"]
    inventory.view()  # Ownership/tags/activation must also see a text key.


def test_opaque_key_does_not_overwrite_previous_inventory(tmp_path):
    inventory = Inventory(tmp_path)
    previous = copy.deepcopy(inventory.snapshot)
    with pytest.raises(ApiUnsupported, match="密钥字段格式无法识别"):
        inventory.publish({"rows": [{"key": {"opaque": "private-value"}}],
                           "memberships": [], "warnings": []})
    assert inventory.snapshot == previous
    assert not (tmp_path / "inventory.json").exists()


def test_scraper_excludes_unrecognised_order_from_reveal_targets():
    scraper = ApiScraper(MagicMock(), ApiOptions(reveal_keys=False, dry_run=True))
    bad = order({"opaque": "private-value"})
    good = order({"key_val": "AAAAA-BBBBB-CCCCC"})
    scraper._list_gamekeys = MagicMock(return_value=["bad", "good"])
    scraper._get_order = MagicMock(side_effect=[bad, good])
    games, stats = scraper.scrape()
    assert len(stats.errors) == 1
    assert "private-value" not in stats.errors[0]
    assert scraper.orders == [good]
    assert games[0].key == "AAAAA-BBBBB-CCCCC"
