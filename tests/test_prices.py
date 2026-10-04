import json
from unittest.mock import patch

from humble_bundle_keys.prices import PriceStore, steam_low


def low(amount=3, currency="CNY", shop=61):
    return {"shop": {"id": shop}, "price": {"amount": amount, "currency": currency},
            "timestamp": "2019-06-26T00:00:00Z"}


def test_only_steam_cny_prices():
    assert steam_low([low(currency="USD"), low(shop=35)]) is None
    assert steam_low([low(amount=float("nan"))]) is None
    assert steam_low([low(amount=0)])["amount"] == 0


def test_batched_lookup_and_persistent_cache(tmp_path):
    store = PriceStore(tmp_path)
    gid = "018d937f-21e1-728e-86d7-9acb3c59f2bb"
    with patch("humble_bundle_keys.prices.request", side_effect=[
        {"app/620": gid, "app/999": None}, [{"id": gid, "lows": [low()]}]
    ]) as req:
        store.refresh(["620", "999"], "test-credential", lambda: None)
    assert req.call_args_list[0].args[2] == ["app/620", "app/999"]
    assert req.call_args_list[1].args[0].endswith("country=CN&shops=61")
    reloaded = PriceStore(tmp_path)
    rows = reloaded.annotate([{"platform": "steam", "steam_app_id": 620},
                             {"platform": "steam", "steam_app_id": 999},
                             {"platform": "steam"}])
    assert rows[0]["steam_cn_low_amount"] == 3
    assert rows[1]["price_status"] == "no_data"
    assert rows[2]["price_status"] == "missing_appid"


def test_error_retains_prices_and_secret_not_in_state(tmp_path):
    store = PriceStore(tmp_path)
    with patch("humble_bundle_keys.prices.request", return_value={"found": True}):
        store.configure("test-credential")
    store.cache = {"620": {"low": steam_low([low()])}}
    with patch("humble_bundle_keys.prices.request", side_effect=ValueError("ITAD 限流")):
        store.refresh(["620"], store.key, lambda: None)
    assert store.cache["620"]["low"]["amount"] == 3
    assert "test-credential" not in json.dumps(store.summary())
    assert not store.busy


def test_failed_validation_does_not_replace_config(tmp_path):
    store = PriceStore(tmp_path)
    with patch("humble_bundle_keys.prices.request", return_value={"found": True}):
        store.configure("working-credential")
    with patch("humble_bundle_keys.prices.request", side_effect=ValueError("API Key 无效")):
        try:
            store.configure("invalid-credential")
        except ValueError:
            pass
    assert PriceStore(tmp_path).key == "working-credential"
