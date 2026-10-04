import copy
from unittest.mock import MagicMock

from humble_bundle_keys.choice import CHOOSECONTENT_URL, REDEEMKEY_URL
from humble_bundle_keys.modern_api import ModernChoiceAPI


def order():
    return {"gamekey": "synthetic", "product": {"machine_name": "april_2026_choice",
            "human_name": "April 2026 Humble Choice", "category": "subscriptioncontent"},
            "tpkd_dict": {"all_tpks": []}}


CARD = {"title": "Synthetic Game", "identifier": "syntheticgame", "platform": "steam"}
URL = "https://www.humblebundle.com/membership/april-2026"


def tpk():
    return {"human_name": CARD["title"], "machine_name": "syntheticgame_choice_steam",
            "key_type": "steam", "keyindex": 0}


def test_allocate_missing_entry_then_reveal_without_modal_click():
    current = order()
    context = MagicMock()
    api = ModernChoiceAPI(context, MagicMock(), lambda *_: copy.deepcopy(current))

    def post(url, body, **kwargs):
        if url == CHOOSECONTENT_URL:
            assert "chosen_identifiers%5B%5D=syntheticgame" in body
            current["tpkd_dict"]["all_tpks"] = [tpk()]
            return {"success": True}
        assert url == REDEEMKEY_URL and "keyindex=0" in body
        return {"key": "AAAAA-BBBBB-CCCCC"}

    api.client._post_form = MagicMock(side_effect=post)
    result = api.claim(current, CARD, URL)
    assert result.status == "success" and result.revealed
    assert api.client._post_form.call_count == 2
    context.new_page.assert_not_called()


def test_uncertain_choose_never_repeats_or_proceeds_to_redeem():
    current = order()
    api = ModernChoiceAPI(MagicMock(), MagicMock(), lambda *_: copy.deepcopy(current))

    def timeout(*args, **kwargs):
        current["tpkd_dict"]["all_tpks"] = [tpk()]
        raise TimeoutError()

    api.client._post_form = MagicMock(side_effect=timeout)
    assert api.claim(current, CARD, URL).status == "uncertain"
    assert api.client._post_form.call_count == 1


def test_timeout_recovers_returned_key_by_reading_only():
    current = order()
    api = ModernChoiceAPI(MagicMock(), MagicMock(), lambda *_: copy.deepcopy(current))

    def timeout(*args, **kwargs):
        current["tpkd_dict"]["all_tpks"] = [{**tpk(), "redeemed_key_val": "SYNTHETIC-KEY"}]
        raise TimeoutError()

    api.client._post_form = MagicMock(side_effect=timeout)
    assert api.claim(current, CARD, URL).key == "SYNTHETIC-KEY"
    assert api.client._post_form.call_count == 1


def test_limited_month_and_missing_metadata_do_not_send_requests():
    current = order()
    api = ModernChoiceAPI(MagicMock(), MagicMock(), MagicMock())
    api.client._post_form = MagicMock()
    limited = order()
    limited["product"].update(machine_name="december_2020_choice",
                              human_name="December 2020 Humble Choice")
    old_url = 'https://www.humblebundle.com/membership/december-2020'
    assert api.claim(limited, CARD, old_url).status == "fallback"
    assert api.claim(current, {"title": CARD["title"]}, URL).status == "fallback"
    api.client._post_form.assert_not_called()
    api.read_order.assert_not_called()
