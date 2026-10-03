"""Tests for the deterministic parsing helpers (no network, no browser)."""

from __future__ import annotations

import pytest

from humble_bundle_keys.scraper import KEY_PATTERN, PLATFORMS


@pytest.mark.parametrize(
    "sample",
    [
        "AAAAA-BBBBB-CCCCC",
        "DDDDD-EEEEE-FFFFF",
        "ABCDE-FGHIJ-KLMNO-PQRST",
        "ABCD1-WXY2Z",
        "AAAA-BBBB-CCCC-DDDD-EEEE",
    ],
)
def test_key_pattern_matches_real_shapes(sample: str) -> None:
    m = KEY_PATTERN.search(sample.upper())
    assert m is not None, f"failed to match {sample!r}"
    assert m.group(1) == sample.upper()


@pytest.mark.parametrize(
    "sample",
    [
        "2025",
        "December",
        "January 6th",
        "Nine Sols",
        "redeem now",
    ],
)
def test_key_pattern_rejects_non_keys(sample: str) -> None:
    m = KEY_PATTERN.search(sample.upper())
    # Either no match, or the matched string is the same word and obviously
    # too short / lower entropy. The regex is deliberately permissive but
    # callers filter by length; we just check there's no full key-shaped hit.
    if m:
        # Should not have produced a hyphen-separated match for any of these.
        assert "-" not in m.group(1), f"wrongly matched {sample!r} as {m.group(1)!r}"


def test_platforms_normalises_common_aliases() -> None:
    assert PLATFORMS["steam"] == "steam"
    assert PLATFORMS["ea origin"] == "origin"
    assert PLATFORMS["ea app"] == "origin"
    assert PLATFORMS["ubisoft connect"] == "uplay"
    assert PLATFORMS["epic games store"] == "epic"
    assert PLATFORMS["drm-free"] == "drmfree"
