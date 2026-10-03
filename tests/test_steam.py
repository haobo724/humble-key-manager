"""Steam integration regressions; all sessions, libraries and keys are synthetic."""
import json
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from humble_bundle_keys.api import _extract_tpk
from humble_bundle_keys.steam import (
    SteamState,
    activate_batch,
    activation_candidates,
    hydrate_app_ids,
    key_digest,
    parse_activation,
    row_id,
    sync_library,
)
from humble_bundle_keys.web import Inventory, make_handler


def row(title="Example", app_id=10, key="AAAAA-BBBBB-CCCCC", **kwargs):
    return {"game_title": title, "steam_app_id": app_id, "key": key,
            "platform": "steam", "humble_url": "https://www.humblebundle.com/downloads?key=test",
            "bundle_name": "Example Bundle", **kwargs}


@pytest.fixture
def steam(tmp_path):
    state = SteamState(tmp_path)
    state.set_account("76561198000000001", "Test Account")
    state.set_library([10, 11])
    return state


def test_appid_extraction_is_exact_not_title_inference():
    assert _extract_tpk({"human_name": "Example", "key_type": "steam",
                         "steam_app_id": "123"}, {}).steam_app_id == 123
    for value in [None, True, 0, "bad", [123]]:
        assert _extract_tpk({"key_type": "steam", "steam_app_id": value}, {}).steam_app_id is None
    assert _extract_tpk({"key_type": "gog", "steam_app_id": 123}, {}).steam_app_id is None


def test_old_inventory_hydrates_appid_without_network(tmp_path):
    cache = tmp_path / "orders-cache"
    cache.mkdir()
    (cache / "test.json").write_text(json.dumps({"gamekey": "test", "tpkd_dict": {
        "all_tpks": [{"human_name": "Example", "key_type": "steam", "steam_app_id": 10}]
    }}), encoding="utf8")
    rows = [row(app_id=None)]
    hydrate_app_ids(rows, tmp_path)
    assert rows[0]["steam_app_id"] == 10


def test_ownership_never_claims_key_was_used(steam):
    rows = steam.annotate([row(), row("Other", 12), row("Unknown", None)])
    assert [r["steam_ownership"] for r in rows] == ["owned", "not_owned", "unknown"]
    assert all(r["activation_status"] == "unknown" for r in rows)
    steam.record("AAAAA-BBBBB-CCCCC", "already_owned")
    assert steam.annotate([row()])[0]["activation_status"] == "already_owned"


def test_account_switch_isolates_library_and_receipts(steam):
    steam.record("AAAAA-BBBBB-CCCCC", "activated")
    steam.set_account("76561198000000002", "Other Account")
    assert steam.library() is None
    assert steam.result("AAAAA-BBBBB-CCCCC") is None
    foreign = steam.annotate([row()])[0]
    assert foreign["activation_status"] == "activated_elsewhere"
    assert foreign["steam_ownership"] == "unknown"
    steam.set_account("76561198000000001", "Test Account")
    assert steam.result("AAAAA-BBBBB-CCCCC")["status"] == "activated"


@pytest.mark.parametrize("data,status", [
    ({"success": 1}, "activated"),
    ({"success": 0, "purchase_result_details": 9}, "already_owned"),
    ({"purchase_result_details": 15}, "duplicate"),
    ({"purchase_result_details": 14}, "invalid"),
    ({"purchase_result_details": 53}, "rate_limited"),
    ({"purchase_result_details": 13}, "region_restricted"),
    ({"purchase_result_details": 24}, "missing_base_game"),
    ({}, "uncertain"), (None, "uncertain"),
])
def test_official_result_codes(data, status):
    assert parse_activation(data) == status


def test_selection_excludes_owned_expired_nonsteam_and_deduplicates(steam):
    rows = [row(), row("Expired", 20, "DDDDD-EEEEE-FFFFF", deadline_state="expired"),
            row("GOG", 20, platform="gog"), row("Hidden", 20, ""),
            row("Eligible", 20, "GGGGG-HHHHH-IIIII"),
            row("Duplicate Key", 21, "GGGGG-HHHHH-IIIII"), row("Bad", 22, "not-key")]
    candidates, skipped = activation_candidates(rows, [row_id(r) for r in rows], steam)
    assert [r["game_title"] for r in candidates] == ["Eligible"]
    assert len(skipped) == 6
    with pytest.raises(ValueError):
        activation_candidates(rows, ["stale"], steam)


def test_changed_key_invalidates_selection_and_previous_attempt_survives_restart(steam, tmp_path):
    first = row("Example", 20)
    assert row_id(first) != row_id({**first, "key": "XXXXX-YYYYY-ZZZZZ"})
    steam.record(first["key"], "uncertain")
    loaded = SteamState(tmp_path)
    candidates, _ = activation_candidates([first], [row_id(first)], loaded)
    assert candidates == []
    text = (tmp_path / "steam.json").read_text(encoding="utf8")
    assert first["key"] not in text
    assert key_digest(first["key"]) in text


def test_library_parse_failure_preserves_snapshot(steam):
    page = MagicMock()
    page.evaluate.return_value = {"status": 200, "data": {}}
    with pytest.raises(RuntimeError):
        sync_library(page, steam)
    assert steam.library()["apps"] == [10, 11]
    page.evaluate.return_value = {"status": 200, "data": {"rgOwnedApps": []}}
    sync_library(page, steam)
    assert steam.library()["apps"] == []


def batch_fixtures(steam):
    context, page, inventory = MagicMock(), MagicMock(), MagicMock()
    context.cookies.return_value = [{"name": "sessionid", "value": "synthetic-session"}]
    inventory.stop_event = threading.Event()
    rows = [row("First", 20), row("Second", 21, "DDDDD-EEEEE-FFFFF")]
    return context, page, inventory, rows


def test_activation_stops_on_limit_without_trying_remaining_keys(steam):
    context, page, inventory, rows = batch_fixtures(steam)
    with patch("humble_bundle_keys.steam.account_from_context", return_value=steam.account()), \
            patch("humble_bundle_keys.steam.submit_key", return_value={
                "status": 200, "data": {"purchase_result_details": 53}}) as submit:
        activate_batch(page, context, steam, rows, [row_id(r) for r in rows], inventory)
    assert submit.call_count == 1
    assert steam.result(rows[0]["key"])["status"] == "rate_limited"
    assert steam.result(rows[1]["key"]) is None


def test_network_uncertainty_is_persisted_and_never_retried_automatically(steam):
    context, page, inventory, rows = batch_fixtures(steam)
    with patch("humble_bundle_keys.steam.account_from_context", return_value=steam.account()), \
            patch("humble_bundle_keys.steam.submit_key", side_effect=TimeoutError) as submit:
        activate_batch(page, context, steam, rows, [row_id(r) for r in rows], inventory)
    assert submit.call_count == 1
    assert steam.result(rows[0]["key"])["status"] == "uncertain"
    assert "AAAAA" not in str(inventory.log.call_args_list)


def test_account_mismatch_submits_nothing(steam):
    context, page, inventory, rows = batch_fixtures(steam)
    with patch("humble_bundle_keys.steam.account_from_context", return_value={
        "steamid": "76561198000000002", "name": "Other"}), \
            patch("humble_bundle_keys.steam.submit_key") as submit, pytest.raises(RuntimeError):
        activate_batch(page, context, steam, rows, [row_id(r) for r in rows], inventory)
    submit.assert_not_called()


def test_success_receipt_persists_and_updates_owned_state(steam):
    context, page, inventory, rows = batch_fixtures(steam)
    inventory.stop_event = MagicMock()
    inventory.stop_event.is_set.return_value = False
    inventory.stop_event.wait.return_value = True
    with patch("humble_bundle_keys.steam.account_from_context", return_value=steam.account()), \
            patch("humble_bundle_keys.steam.submit_key", return_value={
                "status": 200, "data": {"success": 1}}):
        activate_batch(page, context, steam, rows, [row_id(r) for r in rows], inventory)
    assert steam.result(rows[0]["key"])["status"] == "activated"
    assert steam.annotate(rows)[0]["steam_ownership"] == "owned"
    assert "AAAAA" not in str(inventory.log.call_args_list)


def test_activation_preview_has_no_keys_and_has_revision(tmp_path):
    inventory = Inventory(tmp_path)
    inventory.steam.set_account("76561198000000001", "Test")
    inventory.steam.set_library([10])
    inventory.snapshot["rows"] = [row("Eligible", 20)]
    inventory.bind_humble("synthetic-hb")
    plan = inventory.plan_activation([row_id(inventory.snapshot["rows"][0])])
    assert plan["revision"] == inventory.revision
    assert "AAAAA" not in json.dumps(plan)
    inventory.touch()
    assert not inventory.start("activate", plan["revision"])


def test_http_activation_requires_preview_consent_and_matching_revision(tmp_path):
    inventory = Inventory(tmp_path)
    inventory.steam.set_account("76561198000000001", "Test")
    inventory.steam.set_library([10])
    inventory.snapshot["rows"] = [row("Eligible", 20)]
    inventory.bind_humble("synthetic-hb")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(inventory, "test-token"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(path, body):
        url = f"http://127.0.0.1:{server.server_port}{path}"
        request = Request(url, data=json.dumps(body).encode(), headers={
            "X-Local-Token": "test-token", "Content-Type": "application/json"})
        with urlopen(request) as response:
            return json.load(response)

    try:
        plan = post("/api/activation-preview", {
            "selected_ids": [row_id(inventory.snapshot["rows"][0])]})
        body = {"plan_id": plan["plan_id"], "revision": plan["revision"]}
        with patch.object(inventory, "start", return_value=True) as start:
            for flags in [{}, {"accept_ssa": True}, {"confirm_activation": True}]:
                with pytest.raises(HTTPError) as error:
                    post("/api/activate", {**body, **flags})
                assert error.value.code == 400
            start.assert_not_called()
            post("/api/activate", {**body, "accept_ssa": True, "confirm_activation": True})
            args = start.call_args.args
            assert args[0] == "activate"
            assert args[2]["steamid"] == "76561198000000001"
        inventory.touch()
        with pytest.raises(HTTPError) as error:
            post("/api/activate", {**body, "accept_ssa": True, "confirm_activation": True})
        assert error.value.code == 409
        with pytest.raises(HTTPError):
            request = Request(f"http://127.0.0.1:{server.server_port}/api/activation-preview",
                              data=b'{}', headers={"X-Local-Token": "wrong"})
            urlopen(request)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
