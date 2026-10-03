"""Explicit Choice selection tests with synthetic orders and no live mutations."""
import copy
from unittest.mock import MagicMock, patch

import pytest

from humble_bundle_keys.month_claim import (
    claim_months,
    month_slug,
    preview_months,
    remaining_choices,
    save_modal_key,
    validate_selection,
)
from humble_bundle_keys.web import Inventory

URL = "https://www.humblebundle.com/membership/december-2020"
MODERN_URL = "https://www.humblebundle.com/membership/april-2022"


def make_order(modern=False, remaining=2):
    return {"gamekey": "synthetic", "choices_remaining": remaining,
            "product": {"category": "subscriptioncontent",
                        "machine_name": "april_2022_choice" if modern else "december_2020_choice",
                        "human_name": "April 2022 Humble Choice" if modern
                        else "December 2020 Humble Choice"}, "tpkd_dict": {"all_tpks": []}}


@pytest.mark.parametrize("value,expected", [("3", 3), (0, 0), (None, None), (True, None),
                                            (-1, None), ("unknown", None)])
def test_quota_is_strict(value, expected):
    assert remaining_choices({"choices_remaining": value}) == expected


def test_quota_selection_rejects_unknown_zero_excess_and_changed_titles():
    validate_selection("limited", 2, ["A", "B"], ["A", "B", "C"])
    validate_selection("all_games", None, ["A", "B"], ["A", "B"])
    for policy, quota, titles, cards in [
        ("limited", 0, ["A"], ["A"]), ("limited", None, ["A"], ["A"]),
        ("limited", 1, ["A", "B"], ["A", "B"]), ("unknown", 3, ["A"], ["A"]),
        ("limited", 2, ["A"], ["A", "A"]), ("limited", 2, ["A"], ["B"]),
        ("limited", 2, ["A", "A"], ["A"]),
    ]:
        with pytest.raises(ValueError):
            validate_selection(policy, quota, titles, cards)


def fixtures(tmp_path, monkeypatch, modern=False, remaining=2):
    inventory = Inventory(tmp_path)
    order = make_order(modern, remaining)
    url = MODERN_URL if modern else URL
    inventory.snapshot["memberships"] = [{"url": url, "bundle_name": order["product"]["human_name"],
        "state": "pending", "unclaimed_titles": ["A", "B", "C"], "choices_remaining": remaining,
        "total_games": 3, "claimed_games": 0}]
    context, page, claimer = MagicMock(), MagicMock(), MagicMock()
    context.new_page.return_value = page
    cards = [{"title": title, "claimed": False} for title in ["A", "B", "C"]]
    monkeypatch.setattr("humble_bundle_keys.month_claim.find_orders",
                        lambda *args: {url.rsplit("/", 1)[-1]: copy.deepcopy(order)})
    monkeypatch.setattr("humble_bundle_keys.month_claim.get_json",
                        lambda *args: copy.deepcopy(order))
    monkeypatch.setattr("humble_bundle_keys.month_claim.read_cards",
                        lambda *args: copy.deepcopy(cards))
    monkeypatch.setattr("humble_bundle_keys.month_claim.BrowserChoiceClaimer",
                        lambda *args: claimer)
    inventory.stop_event = MagicMock()
    inventory.stop_event.is_set.return_value = False
    inventory.stop_event.wait.return_value = False

    def claim(page, index, attempt):
        cards[index]["claimed"] = True
        order["choices_remaining"] -= 1
        attempt.key = "AAAAA-BBBBB-CCCCC" if attempt.title == "A" else "DDDDD-EEEEE-FFFFF"
        attempt.success = True
        order["tpkd_dict"]["all_tpks"].append({
            "human_name": attempt.title, "key_type": "steam", "steam_app_id": index + 1,
            "redeemed_key_val": attempt.key})
    claimer._claim_single_card.side_effect = claim
    return inventory, order, context, claimer, cards, url


def test_preview_reads_live_quota_and_never_claims(tmp_path, monkeypatch):
    inv, order, context, claimer, _, url = fixtures(tmp_path, monkeypatch)
    inv.snapshot["memberships"][0]["choices_remaining"] = 99
    targets = preview_months(context, inv, [{"url": url, "titles": ["A", "B"]}])
    assert targets[0]["choices_remaining"] == 2
    assert inv.snapshot["memberships"][0]["choices_remaining"] == 2
    assert order["choices_remaining"] == 2
    claimer._claim_single_card.assert_not_called()


def test_cannot_claim_all_on_limited_month(tmp_path, monkeypatch):
    inv, _, context, claimer, _, url = fixtures(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        preview_months(context, inv, [{"url": url, "all_games": True}])
    claimer._claim_single_card.assert_not_called()


def test_rejected_plan_updates_stale_quota_in_inventory(tmp_path, monkeypatch):
    inv, _, context, claimer, _, url = fixtures(tmp_path, monkeypatch, remaining=0)
    inv.snapshot["memberships"][0]["choices_remaining"] = 99
    with pytest.raises(ValueError):
        preview_months(context, inv, [{"url": url, "titles": ["A"]}])
    assert inv.snapshot["memberships"][0]["choices_remaining"] == 0
    assert inv.snapshot["memberships"][0]["state"] == "exhausted"
    claimer._claim_single_card.assert_not_called()


def test_changed_quota_before_confirmation_claims_nothing(tmp_path, monkeypatch):
    inv, order, context, claimer, _, url = fixtures(tmp_path, monkeypatch)
    targets = preview_months(context, inv, [{"url": url, "titles": ["A"]}])
    order["choices_remaining"] = 1
    with pytest.raises(ValueError, match="已经变化"):
        claim_months(context, inv, targets)
    claimer._claim_single_card.assert_not_called()


def test_inventory_preview_does_not_visit_humble_and_claim_starts_one_month(tmp_path, monkeypatch):
    inv, _, context, claimer, _, url = fixtures(tmp_path, monkeypatch)
    inv.snapshot["memberships"][0]["choice_policy"] = "limited"
    plan = inv.plan_months([{"url": url, "titles": ["A"]}], inv.revision)
    context.new_page.assert_not_called()
    assert plan["source"] == "inventory" and plan["targets"][0]["titles"] == ["A"]
    def no_preflight(*args):
        raise AssertionError("must not preflight all months")
    monkeypatch.setattr("humble_bundle_keys.month_claim.preview_months", no_preflight)
    claim_months(context, inv, plan["targets"])
    assert claimer._claim_single_card.call_count == 1


def test_only_selected_titles_consume_slots_and_save_keys(tmp_path, monkeypatch):
    inv, order, context, claimer, cards, url = fixtures(tmp_path, monkeypatch)
    targets = preview_months(context, inv, [{"url": url, "titles": ["A", "B"]}])
    claim_months(context, inv, targets)
    assert [c.args[2].title for c in claimer._claim_single_card.call_args_list] == ["A", "B"]
    assert order["choices_remaining"] == 0
    assert not cards[2]["claimed"]
    assert inv.snapshot["memberships"][0]["choices_remaining"] == 0
    assert len(inv.snapshot["rows"]) == 2  # unknown modal metadata must not create duplicates
    assert all(r["platform"] == "steam" and r["key"] for r in inv.snapshot["rows"])
    assert "AAAAA" not in (tmp_path / "scan.log").read_text(encoding="utf8")


def test_unlimited_plan_freezes_all_game_titles_even_with_zero_old_quota(tmp_path, monkeypatch):
    inv, _, context, _, _, url = fixtures(tmp_path, monkeypatch, modern=True, remaining=0)
    targets = preview_months(context, inv, [{"url": url, "all_games": True}])
    assert targets[0]["titles"] == ["A", "B", "C"]
    assert targets[0]["choices_remaining"] is None
    assert "all_games" not in targets[0]  # execution uses frozen titles, never a new wildcard


def test_uncertain_failure_consumes_at_most_one_slot_and_refreshes_quota(tmp_path, monkeypatch):
    inv, order, context, claimer, cards, url = fixtures(tmp_path, monkeypatch)
    targets = preview_months(context, inv, [{"url": url, "titles": ["A", "B"]}])

    def uncertain(page, index, attempt):
        order["choices_remaining"] -= 1
        cards[index]["claimed"] = True
        raise TimeoutError("synthetic failure after mutation")
    claimer._claim_single_card.side_effect = uncertain
    claim_months(context, inv, targets)
    assert claimer._claim_single_card.call_count == 1
    assert order["choices_remaining"] == 1
    assert inv.snapshot["memberships"][0]["choices_remaining"] == 1
    assert "已停止" in inv.message
    inv.finish_report()
    summary = inv.view()["operation_report"]
    assert summary["failed"] == 1 and summary["remaining"] == 1
    assert summary["processed"] == 1 and summary["status"] == "failed"


def test_stop_preserves_progress_and_leaves_other_selection_untouched(tmp_path, monkeypatch):
    inv, order, context, claimer, _, url = fixtures(tmp_path, monkeypatch)
    inv.stop_event.wait.return_value = True
    targets = preview_months(context, inv, [{"url": url, "titles": ["A", "B"]}])
    claim_months(context, inv, targets)
    assert claimer._claim_single_card.call_count == 1
    assert order["choices_remaining"] == 1
    assert inv.snapshot["rows"][0]["key"]


@pytest.mark.parametrize("delivery", ["epic", "exhausted"])
def test_unavailable_delivery_continues_without_consuming_quota(tmp_path, monkeypatch, delivery):
    inv, order, context, claimer, cards, url = fixtures(tmp_path, monkeypatch)
    original = claimer._claim_single_card.side_effect
    def claim(page, index, attempt):
        if attempt.title == "A":
            if delivery == "epic":
                attempt.error = "epic account linking required"
            else:
                attempt.key_exhausted = True
                attempt.error = "key exhausted: keys are temporarily exhausted"
        else:
            original(page, index, attempt)
    claimer._claim_single_card.side_effect = claim
    targets = preview_months(context, inv, [{"url": url, "titles": ["A", "B"]}])
    claim_months(context, inv, targets)
    inv.finish_report()
    summary = inv.view()["operation_report"]
    assert summary["skipped"] == summary["succeeded"] == 1
    assert summary["failed"] == 0 and summary["status"] == "success"
    assert order["choices_remaining"] == 1 and not cards[0]["claimed"]


def test_saved_modal_key_survives_even_when_order_refresh_fails(tmp_path):
    inv = Inventory(tmp_path)
    save_modal_key(inv, make_order(), "A", "AAAAA-BBBBB-CCCCC")
    reloaded = Inventory(tmp_path)
    assert reloaded.snapshot["rows"][0]["key"] == "AAAAA-BBBBB-CCCCC"


def test_month_urls_reject_external_and_unexpected_paths():
    assert month_slug(URL) == "december-2020"
    for url in ["https://evil.example/membership/december-2020", URL + "/../../account", "bad"]:
        with pytest.raises(ValueError):
            month_slug(url)


def test_quota_drop_mid_batch_stops_before_consuming_another_slot(tmp_path, monkeypatch):
    inv, order, context, claimer, _, url = fixtures(tmp_path, monkeypatch, remaining=3)
    original = claimer._claim_single_card.side_effect

    def claim_with_external_change(page, index, attempt):
        original(page, index, attempt)
        order["choices_remaining"] = 0
    claimer._claim_single_card.side_effect = claim_with_external_change
    targets = preview_months(context, inv, [{"url": url, "titles": ["A", "B"]}])
    with pytest.raises(ValueError):
        claim_months(context, inv, targets)
    assert claimer._claim_single_card.call_count == 1
    assert inv.snapshot["rows"][0]["key"]


def test_claim_http_requires_server_plan_and_explicit_confirmation(tmp_path):
    # Reuse a compact in-memory request handler: rejected calls must never start work.
    import json
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from humble_bundle_keys.web import make_handler

    inv = Inventory(tmp_path)
    inv.month_plan = {"plan_id": "synthetic-plan", "revision": 0,
                      "targets": [{"url": URL, "titles": ["A"]}]}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(inv, "test-token"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(body):
        request = Request(f"http://127.0.0.1:{server.server_port}/api/claim-months",
                          data=json.dumps(body).encode(), headers={"X-Local-Token": "test-token"})
        with urlopen(request) as response:
            return json.load(response)
    try:
        with patch.object(inv, "start", return_value=True) as start:
            for body in [{"plan_id": "synthetic-plan", "revision": 0},
                         {"plan_id": "forged", "revision": 0, "confirm_claim": True}]:
                with pytest.raises(HTTPError):
                    post(body)
            start.assert_not_called()
            post({"plan_id": "synthetic-plan", "revision": 0, "confirm_claim": True,
                  "targets": [{"url": URL, "titles": ["unapproved"]}]})
            assert start.call_args.args[2]["targets"][0]["titles"] == ["A"]
        inv.touch()
        with pytest.raises(HTTPError) as error:
            post({"plan_id": "synthetic-plan", "revision": 0, "confirm_claim": True})
        assert error.value.code == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
