import threading
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from humble_bundle_keys.api import _extract_tpk
from humble_bundle_keys.choice_policy import choice_policy, upgrade_membership
from humble_bundle_keys.reveal import eligible_tpk, preview_reveal, reveal_all
from humble_bundle_keys.steam import row_id
from humble_bundle_keys.web import Inventory, describe_membership


def order(machine, remaining=0):
    return {"gamekey": "synthetic", "choices_remaining": remaining, "product": {
        "machine_name": machine, "human_name": "Test Humble Choice",
        "category": "subscriptioncontent"}}


@pytest.mark.parametrize("machine,expected", [
    ("january_2022_choice", "limited"), ("february_2022_choice", "all_games"),
    ("march_2026_choice_storefront", "all_games"), ("december_2019_choice", "limited"),
    ("march_2018_monthly", "unknown"), ("unknown_choice", "unknown"),
])
def test_choice_policy_boundary(machine, expected):
    assert choice_policy(order(machine)["product"]) == expected


def test_modern_zero_field_is_not_a_selection_limit():
    cards = [{"title": "Game", "claimed": False}]
    modern = describe_membership(order("february_2022_choice"), cards, "url")
    assert modern["state"] == "pending"
    assert modern["choices_remaining"] is None
    assert modern["choice_policy"] == "all_games"
    old = describe_membership(order("january_2022_choice"), cards, "url")
    assert old["state"] == "exhausted"
    assert old["choices_remaining"] == 0


def test_existing_snapshot_upgrade_repairs_modern_exhausted_state():
    month = {"bundle_name": "March 2026 Humble Choice", "state": "exhausted",
             "choices_remaining": 0, "unclaimed_titles": ["Game"],
             "url": "https://www.humblebundle.com/membership/march-2026"}
    upgrade_membership(month)
    assert month["state"] == "pending"
    assert month["choice_policy"] == "all_games"


def test_eligibility_never_selects_old_or_unknown_choice():
    tpk = {"human_name": "Game", "key_type": "steam", "machine_name": "game_choice_steam"}
    assert not eligible_tpk(order("january_2022_choice", 8), tpk)
    assert not eligible_tpk(order("unknown_choice", 8), tpk)
    assert eligible_tpk(order("february_2022_choice", 0), tpk)
    assert not eligible_tpk(order("february_2022_choice"), {**tpk, "is_expired": True})
    assert not eligible_tpk(order("february_2022_choice"), {
        **tpk, "redeemed_key_val": "TEST-ONLY-KEY"})
    assert not eligible_tpk(order("february_2022_choice"), {
        **tpk, "machine_name": "game_choice_epic_keyless"})


def test_preview_has_no_mutations_and_stale_revision_is_rejected(tmp_path):
    inventory = Inventory(tmp_path)
    preview = preview_reveal(inventory.view())
    assert preview["key_records"] == 0
    assert not inventory.busy
    assert not inventory.start("reveal", expected_revision=999)


def test_runner_reveals_regular_keys_but_never_consumes_legacy_choice(tmp_path, monkeypatch):
    normal = {"gamekey": "normal", "product": {"human_name": "Bundle"},
              "tpkd_dict": {"all_tpks": [{"human_name": "Ordinary Game", "key_type": "steam",
                                          "machine_name": "ordinary_bundle_steam"}]}}
    limited = order("january_2022_choice", 8)
    limited["gamekey"] = "limited"
    limited["tpkd_dict"] = {"all_tpks": [{"human_name": "Choice Game", "key_type": "steam",
                                         "machine_name": "choice_choice_steam"}]}
    games = [_extract_tpk(o["tpkd_dict"]["all_tpks"][0], o) for o in [normal, limited]]
    scraper = MagicMock()
    scraper.orders = [normal, limited]
    scraper.scrape.return_value = (games, SimpleNamespace(errors=[]))
    scraper._reveal.return_value = "TEST-ONLY-KEY"
    monkeypatch.setattr("humble_bundle_keys.reveal.ApiScraper", lambda *args: scraper)
    monkeypatch.setattr("humble_bundle_keys.reveal.time.sleep", lambda seconds: None)
    inventory = Inventory(tmp_path)
    inventory.snapshot["rows"] = [asdict(g) for g in games]
    inventory.stop_event = threading.Event()
    reveal_all(None, inventory)
    scraper._reveal.assert_called_once_with(normal["tpkd_dict"]["all_tpks"][0], normal)
    rows = inventory.snapshot["rows"]
    assert rows[0]["key"] == "TEST-ONLY-KEY"
    assert rows[1]["key"] == ""
    assert "TEST-ONLY-KEY" not in (tmp_path / "scan.log").read_text(encoding="utf-8")


def test_modern_zero_budget_claims_missing_tpk_and_saves_key(tmp_path, monkeypatch):
    modern = order("february_2022_choice", 0)
    scraper = MagicMock()
    scraper.orders = [modern]
    scraper.scrape.return_value = ([], SimpleNamespace(errors=[]))
    monkeypatch.setattr("humble_bundle_keys.reveal.ApiScraper", lambda *args: scraper)
    page = MagicMock()
    page.locator.return_value.count.return_value = 1
    page.locator.return_value.nth.return_value.get_attribute.return_value = "content-choice"
    context = MagicMock()
    context.new_page.return_value = page
    claimer = MagicMock()
    claimer._read_title.return_value = "New Game"

    def claim(page, index, attempt):
        attempt.key = "TEST-CHOICE-KEY"
        attempt.success = True

    claimer._claim_single_card.side_effect = claim
    monkeypatch.setattr("humble_bundle_keys.reveal.BrowserChoiceClaimer", lambda *args: claimer)
    monkeypatch.setattr("humble_bundle_keys.reveal.time.sleep", lambda seconds: None)
    inventory = Inventory(tmp_path)
    reveal_all(context, inventory)
    claimer._claim_single_card.assert_called_once()
    assert inventory.snapshot["rows"][0]["key"] == "TEST-CHOICE-KEY"
    assert "TEST-CHOICE-KEY" not in (tmp_path / "scan.log").read_text(encoding="utf-8")


def test_selected_reveal_does_not_touch_unselected_keys(tmp_path, monkeypatch):
    normal = {"gamekey": "normal", "product": {"human_name": "Bundle"},
              "tpkd_dict": {"all_tpks": [
                  {"human_name": title, "key_type": "steam", "machine_name": "game_bundle_steam"}
                  for title in ["Selected Game", "Unselected Game"]]}}
    games = [_extract_tpk(tpk, normal) for tpk in normal["tpkd_dict"]["all_tpks"]]
    scraper = MagicMock()
    scraper.orders = [normal]
    scraper.scrape.return_value = (games, SimpleNamespace(errors=[]))
    scraper._reveal.return_value = "TEST-ONLY-KEY"
    monkeypatch.setattr("humble_bundle_keys.reveal.ApiScraper", lambda *args: scraper)
    monkeypatch.setattr("humble_bundle_keys.reveal.time.sleep", lambda seconds: None)
    inventory = Inventory(tmp_path)
    inventory.snapshot["rows"] = [asdict(g) for g in games]
    reveal_all(None, inventory, [row_id(inventory.snapshot["rows"][0])])
    scraper._reveal.assert_called_once_with(normal["tpkd_dict"]["all_tpks"][0], normal)
    assert inventory.snapshot["rows"][1]["key"] == ""
