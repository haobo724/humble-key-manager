from humble_bundle_keys.hb_activation import HBActivationState
from humble_bundle_keys.steam import SteamState
from humble_bundle_keys.web import Inventory


def row():
    return {"game_title": "Synthetic Game", "key": "AAAAA-BBBBB-CCCCC", "platform": "steam",
            "humble_url": "https://www.humblebundle.com/downloads?key=synthetic",
            "bundle_name": "Synthetic Bundle"}


def test_success_mark_survives_restart_rescan_and_steam_switch(tmp_path):
    inv = Inventory(tmp_path)
    inv.snapshot["rows"] = [row()]
    inv.steam.set_account("76561198000000001", "Synthetic")
    inv.bind_humble("hb-account-a")
    inv.steam.record(row()["key"], "activated")
    inv.mark_hb_activation(row(), "76561198000000001")
    loaded = HBActivationState(tmp_path)
    steam = SteamState(tmp_path)
    steam.set_account("76561198000000002", "Other")
    loaded.bind("hb-account-a", [row()], steam)
    assert loaded.annotate([row()])[0]["system_activated"]
    assert row()["key"] not in loaded.path.read_text(encoding="utf-8")


def test_hb_account_isolation_and_legacy_migration(tmp_path):
    steam = SteamState(tmp_path)
    steam.set_account("76561198000000001", "Synthetic")
    steam.record(row()["key"], "activated")
    marks = HBActivationState(tmp_path)
    marks.bind("account-a", [row()], steam)
    assert marks.annotate([row()])[0]["system_activated"]
    marks.bind("account-b", [row()], steam)
    assert not marks.annotate([row()])[0]["system_activated"]
    marks.bind("account-a", [row()], steam)
    assert marks.annotate([row()])[0]["system_activated"]


def test_owned_and_duplicate_results_are_not_success_marks(tmp_path):
    steam = SteamState(tmp_path)
    steam.set_account("76561198000000001", "Synthetic")
    marks = HBActivationState(tmp_path)
    for status in ["already_owned", "duplicate", "uncertain"]:
        steam.record(row()["key"], status)
        marks.bind("account-a", [row()], steam)
        assert not marks.annotate([row()])[0]["system_activated"]
