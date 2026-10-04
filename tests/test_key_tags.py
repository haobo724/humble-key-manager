import pytest

from humble_bundle_keys.key_tags import KeyTags
from humble_bundle_keys.steam import SteamState, activation_candidates, row_id
from humble_bundle_keys.web import Inventory


def row(key="AAAAA-BBBBB-CCCCC"):
    return {"game_title": "Demo", "bundle_name": "Demo Bundle", "platform": "steam",
            "humble_url": "https://example.test/order", "key": key, "steam_app_id": 10}


def test_tags_persist_across_reveal_and_stay_in_account(tmp_path):
    tags = KeyTags(tmp_path)
    tags.set("account-a", [row("")], ["卖掉了", "朋友"], False)
    tags = KeyTags(tmp_path)
    result = tags.annotate([row()], "account-a")[0]
    assert result["custom_tags"] == ["卖掉了", "朋友"]
    assert result["activation_excluded"]
    assert not tags.annotate([row()], "account-b")[0]["custom_tags"]
    assert "AAAAA" not in tags.path.read_text(encoding="utf-8")
    tags.set("account-a", [row()], [], False)
    assert not KeyTags(tmp_path).annotate([row()], "account-a")[0]["activation_excluded"]


@pytest.mark.parametrize("tags,excluded", [(["卖掉了"], False), (["留给朋友"], True)])
def test_tagged_keys_are_skipped_in_activation_candidates(tmp_path, tags, excluded):
    ledger = KeyTags(tmp_path)
    ledger.set("account", [row()], tags, excluded)
    steam = SteamState(tmp_path)
    steam.set_account("76561198000000001", "Demo")
    steam.set_library([])
    candidates, skipped = activation_candidates(
        ledger.annotate([row()], "account"), [row_id(row())], steam)
    assert not candidates
    assert len(skipped) == 1 and "标签" in skipped[0]["reason"]


def test_tag_update_invalidates_plan_and_rejects_busy_or_stale_edit(tmp_path):
    inv = Inventory(tmp_path)
    inv.hb_activation.data["current_account"] = "account"
    inv.snapshot["rows"] = [row()]
    inv.activation_plan = {"id": "old"}
    inv.set_tags([row_id(row())], ["自己的"], False, inv.revision)
    assert inv.activation_plan is None
    assert inv.view()["rows"][0]["custom_tags"] == ["自己的"]
    with pytest.raises(ValueError):
        inv.set_tags([row_id(row())], [], False, 0)
    inv.busy = True
    with pytest.raises(ValueError):
        inv.set_tags([row_id(row())], [], False, inv.revision)
