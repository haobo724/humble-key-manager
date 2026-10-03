"""Tests for the browser-driven Choice claim flow.

The class itself drives a real Playwright page so it can't be tested
without a live account. These tests cover the deterministic helpers —
slug derivation and target discovery — which are pure functions on
order JSON.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from humble_bundle_keys.browser_choice import (
    BrowserChoiceClaimer,
    BrowserClaimAttempt,
    BrowserClaimOptions,
    derive_membership_slug,
)


def test_epic_delivery_skips_before_clicking_redeem_button():
    page = MagicMock()
    modal = page.locator.return_value.first
    modal.evaluate.return_value = True
    claimer = _claimer()
    attempt = BrowserClaimAttempt(slug="april-2024", title="Epic game")
    claimer._claim_single_card(page, 0, attempt)
    assert attempt.error == "epic account linking required"
    assert not attempt.key
    modal.locator.assert_not_called()  # no redemption control was even selected


def test_exhausted_keys_skip_before_clicking_redeem_button():
    page = MagicMock()
    modal = page.locator.return_value.first
    modal.evaluate.return_value = False
    modal.inner_text.return_value = "Keys are temporarily exhausted for this product"
    attempt = BrowserClaimAttempt(slug="april-2024", title="Game")
    _claimer()._claim_single_card(page, 0, attempt)
    assert attempt.key_exhausted and not attempt.key
    modal.locator.assert_not_called()


@pytest.mark.parametrize(
    "product,expected",
    [
        # Explicit choice_url wins
        ({"choice_url": "december-2022", "machine_name": "december_2022_choice"}, "december-2022"),
        # Choice machine_name conversion
        ({"machine_name": "march_2026_choice"}, "march-2026"),
        ({"machine_name": "june_2025_choice"}, "june-2025"),
        # Storefront suffix
        ({"machine_name": "august_2023_choice_storefront"}, "august-2023"),
        # Legacy monthly
        ({"machine_name": "april_2018_monthly"}, "april-2018"),
        ({"machine_name": "august_2019_monthly"}, "august-2019"),
        # Empty / nonsense
        ({}, None),
        ({"machine_name": ""}, None),
        ({"machine_name": "weird_thing"}, "weird-thing"),  # falls through, kebab'd
        (None, None),
    ],
)
def test_derive_membership_slug(product, expected) -> None:
    assert derive_membership_slug(product) == expected


def test_derive_membership_slug_strips_explicit_url_slashes() -> None:
    assert derive_membership_slug({"choice_url": "/december-2022/"}) == "december-2022"


# ---------------------------------------------------------------------------
# Target discovery — uses _discover_targets without spinning up a browser
# ---------------------------------------------------------------------------

class _StubContext:
    """Minimal stand-in for BrowserContext — never actually used by the
    methods we test."""

    request = None  # the methods we exercise don't touch this


def _claimer() -> BrowserChoiceClaimer:
    return BrowserChoiceClaimer(_StubContext(), BrowserClaimOptions())


def test_discover_targets_includes_choice_orders() -> None:
    orders = [
        {
            "gamekey": "abc",
            "product": {"category": "subscriptioncontent", "machine_name": "march_2026_choice"},
        },
        {
            "gamekey": "def",
            "product": {"category": "subscriptioncontent", "machine_name": "april_2018_monthly"},
        },
    ]
    targets = _claimer()._discover_targets(orders)
    slugs = sorted(s for _o, s in targets)
    assert slugs == ["april-2018", "march-2026"]


def test_discover_targets_skips_non_subscription() -> None:
    orders = [
        {
            "gamekey": "abc",
            "product": {"category": "bundle", "machine_name": "indie_megabundle"},
        },
        {
            "gamekey": "def",
            "product": {"category": "storefront", "machine_name": "free_game_storefront"},
        },
    ]
    targets = _claimer()._discover_targets(orders)
    assert targets == []


def test_discover_targets_dedupes_slugs() -> None:
    """Two orders with the same slug — only one membership page to visit."""
    orders = [
        {
            "gamekey": "abc",
            "product": {"category": "subscriptioncontent", "machine_name": "march_2026_choice"},
        },
        {
            "gamekey": "def",
            "product": {
                "category": "subscriptioncontent",
                "machine_name": "march_2026_choice",  # dupe
            },
        },
    ]
    targets = _claimer()._discover_targets(orders)
    assert len(targets) == 1
    assert targets[0][1] == "march-2026"


def test_discover_targets_prefers_explicit_choice_url() -> None:
    orders = [
        {
            "gamekey": "abc",
            "product": {
                "category": "subscriptioncontent",
                "machine_name": "weird_internal_codename",  # bad slug derivation
                "choice_url": "june-2025",  # but the canonical URL is provided
            },
        },
    ]
    targets = _claimer()._discover_targets(orders)
    assert targets[0][1] == "june-2025"
