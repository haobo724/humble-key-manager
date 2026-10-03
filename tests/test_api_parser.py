"""Tests for the JSON-API parser (no network)."""

from __future__ import annotations

from humble_bundle_keys.api import (
    _expiry_to_deadline,
    _extract_tpk,
    _normalise_platform,
)

SAMPLE_ORDER = {
    "gamekey": "abc123XYZ",
    "human_name": "December 2025 Humble Choice",
    "machine_name": "december_2025_choice_storefront",
    "created": "2025-12-03T17:00:00",
    "tpkd_dict": {
        "all_tpks": [
            {
                "machine_name": "ninesols_steam",
                "human_name": "Nine Sols",
                "key_type": "steam",
                "key_type_human_name": "Steam",
                "redeemed_key_val": None,
                "is_expired": False,
                "expiry_date": "2027-01-06T18:00:00",
                "key_index": 0,
                "instructions_html": "Available on Windows.",
            },
            {
                "machine_name": "lad_gaiden_steam",
                "human_name": "Like a Dragon Gaiden: The Man Who Erased His Name",
                "key_type": "steam",
                "redeemed_key_val": "AAAAA-BBBBB-CCCCC",
                "is_expired": False,
                "expiry_date": "2027-01-06T18:00:00",
                "key_index": 1,
                "instructions_html": "Available on Windows and macOS.",
            },
        ]
    },
}


def test_extract_tpk_revealed() -> None:
    tpk = SAMPLE_ORDER["tpkd_dict"]["all_tpks"][1]
    gk = _extract_tpk(tpk, SAMPLE_ORDER)
    assert gk.game_title == "Like a Dragon Gaiden: The Man Who Erased His Name"
    assert gk.platform == "steam"
    assert gk.key == "AAAAA-BBBBB-CCCCC"
    assert gk.redeemed_on_humble is True
    assert "Windows" in gk.os_support
    assert "macOS" in gk.os_support
    assert gk.bundle_name == "December 2025 Humble Choice"
    assert "abc123XYZ" in gk.humble_url


def test_extract_tpk_unrevealed() -> None:
    tpk = SAMPLE_ORDER["tpkd_dict"]["all_tpks"][0]
    gk = _extract_tpk(tpk, SAMPLE_ORDER)
    assert gk.game_title == "Nine Sols"
    assert gk.key == ""
    assert gk.redeemed_on_humble is False
    assert gk.platform == "steam"


def test_extract_tpk_handles_missing_human_name() -> None:
    tpk = {
        "machine_name": "foo_bar",
        "key_type": "steam",
        "redeemed_key_val": None,
        "keyindex": 0,
    }
    gk = _extract_tpk(tpk, SAMPLE_ORDER)
    # Falls back to machine_name
    assert gk.game_title == "foo_bar"


def test_extract_tpk_uses_nested_product_for_bundle_name() -> None:
    """Order metadata lives under `product` in the real API, not at root."""
    order = {
        "gamekey": "abc",
        "product": {
            "category": "subscriptioncontent",
            "machine_name": "august_2019_monthly",
            "human_name": "August 2019 Humble Monthly",
        },
    }
    tpk = {
        "machine_name": "kingdomcome_deliverance_monthly_steam",
        "human_name": "Kingdom Come: Deliverance",
        "key_type": "steam",
        "redeemed_key_val": None,
        "keyindex": 0,
    }
    gk = _extract_tpk(tpk, order)
    assert gk.bundle_name == "August 2019 Humble Monthly"


def test_normalise_platform_known() -> None:
    assert _normalise_platform("steam") == "steam"
    assert _normalise_platform("Steam") == "steam"
    assert _normalise_platform("EA Origin") == "origin"
    assert _normalise_platform("Ubisoft Connect") == "uplay"


def test_normalise_platform_unknown_returns_blank() -> None:
    # Anything we don't recognise that isn't a clean short alpha string returns ''
    assert _normalise_platform(None) == ""
    assert _normalise_platform("") == ""
    # Allow short, alpha-only strings through (e.g. new platforms we haven't added)
    assert _normalise_platform("steamdeck") == "steamdeck"
    # But reject things with spaces / punctuation we don't know
    assert _normalise_platform("Some Long Platform Name 9000!") == ""


def test_expiry_to_deadline_active() -> None:
    tpk = {"is_expired": False, "expiry_date": "2027-01-06T18:00:00"}
    s = _expiry_to_deadline(tpk)
    assert s.startswith("Must be redeemed by")
    assert "2027" in s


def test_expiry_to_deadline_expired() -> None:
    tpk = {"is_expired": True}
    assert _expiry_to_deadline(tpk) == "Expired"


def test_expiry_to_deadline_missing() -> None:
    tpk = {}
    assert _expiry_to_deadline(tpk) == ""
