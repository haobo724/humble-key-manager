import json

import pytest

from humble_bundle_keys.api import _extract_tpk
from humble_bundle_keys.regions import annotate_regions, hydrate_restrictions


@pytest.mark.parametrize("allow,deny,expected", [
    (["CN", "US"], [], "allowed"), ([], ["CN"], "blocked"),
    (["US"], [], "blocked"), ([], [], "unrestricted"),
    (None, None, "unknown"), (["CN"], None, "unknown"),
    (None, ["CN"], "blocked"), (["CN"], ["CN"], "blocked"),
    ("CN", [], "unknown"), (["China"], [], "unknown"),
])
def test_china_restrictions_use_exact_country_metadata(allow, deny, expected):
    row = {"platform": "steam", "exclusive_countries": allow, "disallowed_countries": deny}
    assert annotate_regions([row])[0]["china_activation"] == expected


def test_cached_regions_upgrade_inventory_without_title_guessing(tmp_path):
    cache = tmp_path / "orders-cache"
    cache.mkdir()
    order = {"gamekey": "synthetic", "tpkd_dict": {"all_tpks": [{
        "human_name": "Demo", "key_type": "steam", "exclusive_countries": [],
        "disallowed_countries": ["cn"], "redeemed_key_val": "AAAAA-BBBBB-CCCCC"}]}}
    game = _extract_tpk(order["tpkd_dict"]["all_tpks"][0], order)
    assert game.disallowed_countries == ["CN"]
    (cache / "synthetic.json").write_text(json.dumps(order), encoding="utf-8")
    row = {"game_title": game.game_title, "humble_url": game.humble_url,
           "platform": game.platform, "key": game.key}
    other = {**row, "key": "DDDDD-EEEEE-FFFFF"}
    assert hydrate_restrictions([row, other], tmp_path)
    assert annotate_regions([row])[0]["china_activation"] == "blocked"
    assert annotate_regions([other])[0]["china_activation"] == "unknown"
    assert "exclusive_countries" not in game.to_row()  # CLI CSV stays compatible.
