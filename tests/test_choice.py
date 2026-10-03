"""Tests for humble_bundle_keys.choice (Humble Choice claim flow).

These cover the deterministic helpers — request body construction, key
extraction from response bodies, machine_name parsing — plus a fixture-based
test that the captured live request bodies round-trip through our builder.
No live network here.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from humble_bundle_keys.choice import (
    build_choosecontent_body,
    build_redeemkey_body,
    categorize_keytype,
    extract_revealed_key,
    is_choice_keytype,
    looks_like_choice_order,
    short_id_for_keytype,
    unclaimed_choice_tpks,
)

FIXTURES = Path(__file__).parent / "fixtures" / "choice_claim"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "machine_name",
    [
        # All four real-world Choice keytype patterns we've observed
        "zerohour_row_choice_steam",  # row modifier
        "etrianodysseyiiihd_row_choice_steam",  # row modifier
        "chantsofsennaar_choice_steam",  # no modifier
        "sworn_choice_steam",  # no modifier
        "tempestrising_naeu_choice_steam",  # regional modifier
        "diplomacyisnotanoption_choice_epic_keyless",  # multi-word platform
    ],
)
def test_is_choice_keytype_recognises_real_patterns(machine_name) -> None:
    assert is_choice_keytype(machine_name) is True


def test_is_choice_keytype_rejects_non_choice() -> None:
    # Legacy Humble Monthly is NOT considered Choice — it uses Flow A directly
    assert is_choice_keytype("kingdomcome_deliverance_monthly_steam") is False
    assert is_choice_keytype("phantomdoctrine_monthly_steam") is False
    # Plain bundle keys
    assert is_choice_keytype("zerohour_steam") is False
    assert is_choice_keytype("plain_origin") is False
    assert is_choice_keytype("") is False
    assert is_choice_keytype(None) is False


@pytest.mark.parametrize(
    "machine_name,expected_short",
    [
        ("zerohour_row_choice_steam", "zerohour"),
        ("etrianodysseyiiihd_row_choice_steam", "etrianodysseyiiihd"),
        ("chantsofsennaar_choice_steam", "chantsofsennaar"),
        ("sworn_choice_steam", "sworn"),
        ("tempestrising_naeu_choice_steam", "tempestrising"),
        ("diplomacyisnotanoption_choice_epic_keyless", "diplomacyisnotanoption"),
    ],
)
def test_short_id_for_keytype_strips_suffix(machine_name, expected_short) -> None:
    assert short_id_for_keytype(machine_name) == expected_short


def test_short_id_for_non_choice_returns_none() -> None:
    assert short_id_for_keytype("zerohour_steam") is None
    assert short_id_for_keytype("kingdomcome_deliverance_monthly_steam") is None


def test_looks_like_choice_order_uses_nested_product() -> None:
    """Order shape from real API: machine_name lives under product."""
    order = {
        "gamekey": "abc123",
        "product": {
            "category": "subscriptioncontent",
            "machine_name": "march_2026_choice",
            "human_name": "March 2026 Humble Choice",
        },
    }
    assert looks_like_choice_order(order) is True


def test_looks_like_choice_order_via_human_name_fallback() -> None:
    order = {
        "product": {
            "category": "subscriptioncontent",
            "machine_name": "weird_legacy_thing",
            "human_name": "March 2026 Humble Choice",
        }
    }
    assert looks_like_choice_order(order) is True


def test_looks_like_choice_order_rejects_random_bundles() -> None:
    order = {
        "product": {
            "category": "bundle",
            "machine_name": "indie_megabundle_2024",
            "human_name": "Indie Megabundle",
        }
    }
    assert looks_like_choice_order(order) is False


def test_looks_like_choice_order_rejects_legacy_monthly() -> None:
    """Legacy Humble Monthly is subscription content but isn't Choice — it uses Flow A."""
    order = {
        "product": {
            "category": "subscriptioncontent",
            "machine_name": "august_2019_monthly",
            "human_name": "August 2019 Humble Monthly",
        }
    }
    assert looks_like_choice_order(order) is False


def test_looks_like_choice_order_tolerates_root_level_fields() -> None:
    """Old API shapes had machine_name at the root. Be tolerant."""
    order = {"machine_name": "march_2026_choice"}
    assert looks_like_choice_order(order) is True


def test_unclaimed_choice_tpks_filters_correctly() -> None:
    order = {
        "machine_name": "march_2026_choice_storefront",
        "human_name": "March 2026 Humble Choice",
        "tpkd_dict": {
            "all_tpks": [
                # Unclaimed Choice game — should be returned
                {
                    "machine_name": "zerohour_row_choice_steam",
                    "human_name": "Zero Hour",
                    "redeemed_key_val": None,
                },
                # Already-claimed Choice game — must be skipped
                {
                    "machine_name": "ninesols_row_choice_steam",
                    "human_name": "Nine Sols",
                    "redeemed_key_val": "ALREADY-HAVE-KEY",
                },
                # Non-Choice content somehow in here — must be skipped
                {
                    "machine_name": "something_steam",
                    "human_name": "Something Else",
                    "redeemed_key_val": None,
                },
            ]
        },
    }
    out = unclaimed_choice_tpks(order)
    assert len(out) == 1
    assert out[0]["machine_name"] == "zerohour_row_choice_steam"


# ---------------------------------------------------------------------------
# Real-world typo: Humble has `_hoice_` (missing c) on some Choice tpks
# ---------------------------------------------------------------------------

def test_is_choice_keytype_matches_hoice_typo() -> None:
    """Humble has a real typo: e.g. road96_europe_hoice_steam (missing 'c')."""
    assert is_choice_keytype("road96_europe_hoice_steam") is True


def test_short_id_for_hoice_typo() -> None:
    assert short_id_for_keytype("road96_europe_hoice_steam") == "road96"


# ---------------------------------------------------------------------------
# categorize_keytype — one-line user-facing diagnosis
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "machine_name,expected",
    [
        # Choice variants
        ("zerohour_row_choice_steam", "choice"),
        ("chantsofsennaar_choice_steam", "choice"),
        ("road96_europe_hoice_steam", "choice"),  # typo'd Choice
        # Legacy monthly
        ("kingdomcome_deliverance_monthly_steam", "monthly"),
        ("laserleague_monthly_steam", "monthly"),
        # Special platforms
        ("worldwarz_gotyedition_builttosurvive_bundle_epic_keyless", "keyless"),
        ("desync_freegame_steam", "freegame"),
        ("kingdomclassic_freegame_steam", "freegame"),
        # Software bundles & vouchers
        ("mixcraft8_acoustica_megasounddesigner_softwarebundle", "softwarebundle"),
        ("bestsyntygamedevassets_syntystore10usdvoucher", "voucher"),
        ("dccomicsfreetrial_november2023choice_coupon", "voucher"),
        # Plain bundle
        ("game_bundle_steam", "bundle"),
        # Unknown
        ("nothing_recognizable", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_categorize_keytype(machine_name, expected) -> None:
    assert categorize_keytype(machine_name) == expected


# ---------------------------------------------------------------------------
# Body builders
# ---------------------------------------------------------------------------

def test_build_choosecontent_body_single_game() -> None:
    body = build_choosecontent_body(
        gamekey="ABC123", short_ids=["zerohour"]
    )
    parsed = parse_qs(body)
    assert parsed["gamekey"] == ["ABC123"]
    assert parsed["parent_identifier"] == ["initial"]
    assert parsed["chosen_identifiers[]"] == ["zerohour"]


def test_build_choosecontent_body_multiple_games() -> None:
    body = build_choosecontent_body(
        gamekey="ABC123", short_ids=["zerohour", "ninesols"]
    )
    parsed = parse_qs(body)
    # parse_qs collects repeated keys into a list
    assert parsed["chosen_identifiers[]"] == ["zerohour", "ninesols"]


def test_build_redeemkey_body_shape() -> None:
    body = build_redeemkey_body(
        gamekey="ABC123", keytype="zerohour_row_choice_steam", keyindex=0
    )
    parsed = parse_qs(body)
    assert parsed["keytype"] == ["zerohour_row_choice_steam"]
    assert parsed["key"] == ["ABC123"]
    assert parsed["keyindex"] == ["0"]


# ---------------------------------------------------------------------------
# Response key extraction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body,expected",
    [
        ({"key": "ABC-DEF-GHI", "success": True}, "ABC-DEF-GHI"),
        ({"redeemed_key_val": "X-Y-Z"}, "X-Y-Z"),
        ({"key_val": "X-Y-Z"}, "X-Y-Z"),
        ({"success": True}, None),  # success but no key
        ({}, None),
        ("not a dict", None),
        (None, None),
    ],
)
def test_extract_revealed_key(body, expected) -> None:
    assert extract_revealed_key(body) == expected


# ---------------------------------------------------------------------------
# Fixture-based: the live captured bodies should match our builders
# ---------------------------------------------------------------------------

def test_choosecontent_fixture_matches_builder() -> None:
    """Our builder reproduces the exact body shape Humble's frontend sent."""
    fixture = json.loads(
        (FIXTURES / "choosecontent_request_response.json").read_text()
    )
    captured = fixture["request_body"]
    # The captured body has REDACTED-GAMEKEY (sanitiser ran), so we
    # reconstruct with that placeholder and compare.
    rebuilt = build_choosecontent_body(
        gamekey="REDACTED-GAMEKEY",
        short_ids=["zerohour"],
        parent_identifier="initial",
    )
    assert parse_qs(captured) == parse_qs(rebuilt)


def test_redeemkey_fixture_matches_builder() -> None:
    fixture = json.loads(
        (FIXTURES / "redeemkey_request_response.json").read_text()
    )
    captured = fixture["request_body"]
    # In the captured body the gamekey is the LITERAL value 'SYNTHETICORDER0000'
    # (16-char alnum, the sanitiser's GAMEKEY_URL_PARAM regex only catches
    # gamekey= form-data when it's a URL parameter, not in form bodies). That's
    # fine — we just reconstruct with the same value to compare shapes.
    captured_parsed = parse_qs(captured)
    rebuilt = build_redeemkey_body(
        gamekey=captured_parsed["key"][0],
        keytype="zerohour_row_choice_steam",
        keyindex=0,
    )
    assert parse_qs(captured) == parse_qs(rebuilt)


def test_choosecontent_fixture_response_is_what_we_expect() -> None:
    fixture = json.loads(
        (FIXTURES / "choosecontent_request_response.json").read_text()
    )
    body = fixture["response_body"]
    assert body == {"force_refresh": True, "success": True}


def test_redeemkey_fixture_response_yields_redacted_key() -> None:
    fixture = json.loads(
        (FIXTURES / "redeemkey_request_response.json").read_text()
    )
    body = fixture["response_body"]
    # Sanitiser replaced the actual key; the field still extracts.
    assert extract_revealed_key(body) == "REDACTED-KEY"
