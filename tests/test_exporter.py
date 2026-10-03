"""Tests for the CSV exporter and merge logic."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from humble_bundle_keys.exporter import merge_with_existing, write_csv
from humble_bundle_keys.models import CSV_HEADERS, GameKey


@pytest.fixture
def sample_rows() -> list[GameKey]:
    return [
        GameKey(
            game_title="Like a Dragon Gaiden: The Man Who Erased His Name",
            platform="steam",
            key="AAAAA-BBBBB-CCCCC",
            bundle_name="December 2025 Humble Choice",
            redemption_deadline="Must be redeemed by January 6th, 2027.",
            redeemed_on_humble=True,
            os_support="Windows",
            humble_url="https://www.humblebundle.com/downloads?key=abc",
        ),
        GameKey(
            game_title="Nine Sols",
            platform="steam",
            key="",
            bundle_name="December 2025 Humble Choice",
            redeemed_on_humble=False,
        ),
    ]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_write_csv_uses_canonical_headers(tmp_path: Path, sample_rows) -> None:
    out = tmp_path / "out.csv"
    write_csv(sample_rows, out)
    rows = _read_csv(out)
    assert list(rows[0].keys()) == CSV_HEADERS


def test_write_csv_enriches_bundle_date_from_bundle_name(tmp_path, sample_rows) -> None:
    out = tmp_path / "out.csv"
    write_csv(sample_rows, out)
    rows = _read_csv(out)
    assert rows[0]["bundle_date"] == "December 2025"
    assert rows[1]["bundle_date"] == "December 2025"


def test_write_csv_serialises_booleans_as_strings(tmp_path, sample_rows) -> None:
    out = tmp_path / "out.csv"
    write_csv(sample_rows, out)
    rows = _read_csv(out)
    assert rows[0]["redeemed_on_humble"] == "true"
    assert rows[1]["redeemed_on_humble"] == "false"


def test_merge_preserves_revealed_key_when_new_run_lacks_it(tmp_path, sample_rows) -> None:
    """If we previously revealed a key and a fresh extract lost it, keep the old key."""
    existing = tmp_path / "existing.csv"
    write_csv(sample_rows, existing)

    # Same identity, but key missing.
    new_rows = [
        GameKey(
            game_title="Like a Dragon Gaiden: The Man Who Erased His Name",
            platform="steam",
            key="",
            bundle_name="December 2025 Humble Choice",
        ),
    ]
    merged = merge_with_existing(new_rows, existing)
    titles = {(r.game_title, r.key) for r in merged}
    # Existing revealed key should be preserved
    assert (
        "Like a Dragon Gaiden: The Man Who Erased His Name",
        "AAAAA-BBBBB-CCCCC",
    ) in titles


def test_merge_adds_brand_new_rows(tmp_path, sample_rows) -> None:
    existing = tmp_path / "existing.csv"
    write_csv(sample_rows, existing)

    new_rows = [
        GameKey(
            game_title="Stray",
            platform="steam",
            key="NEW-KEY-AAAA",
            bundle_name="January 2026 Humble Choice",
        ),
    ]
    merged = merge_with_existing(new_rows, existing)
    titles = {r.game_title for r in merged}
    # New row added; old rows kept
    assert "Stray" in titles
    assert "Nine Sols" in titles


def test_merge_returns_new_rows_when_no_existing_file(tmp_path) -> None:
    new_rows = [GameKey(game_title="X", platform="steam", key="ABC-DEF-GHI")]
    result = merge_with_existing(new_rows, tmp_path / "does-not-exist.csv")
    assert result == new_rows


def test_merge_collapses_when_old_csv_had_blank_bundle_name(tmp_path) -> None:
    """Regression: pre-0.2.1 CSVs had blank bundle_name (we read the wrong API
    field). When the same humble_url shows up post-fix with a populated
    bundle_name, merge must collapse them rather than duplicate."""
    old_csv = tmp_path / "old.csv"
    old_rows = [
        GameKey(
            game_title="Kingdom Come: Deliverance",
            platform="steam",
            key="OLD-KEY-FROM-PREV-RUN",
            bundle_name="",  # ← was blank before the fix
            bundle_date="",
            humble_url="https://www.humblebundle.com/downloads?key=ABC123",
        ),
    ]
    write_csv(old_rows, old_csv)

    new_rows = [
        # Same physical row but with the now-correct bundle_name + date
        GameKey(
            game_title="Kingdom Come: Deliverance",
            platform="steam",
            key="OLD-KEY-FROM-PREV-RUN",  # already revealed; same value
            bundle_name="August 2019 Humble Monthly",
            bundle_date="August 2019",
            humble_url="https://www.humblebundle.com/downloads?key=ABC123",
        ),
    ]
    merged = merge_with_existing(new_rows, old_csv)
    # Must collapse to a single row, not duplicate
    assert len(merged) == 1
    # And the resulting row should have the populated bundle_name
    assert merged[0].bundle_name == "August 2019 Humble Monthly"
    assert merged[0].key == "OLD-KEY-FROM-PREV-RUN"


def test_merge_uses_humble_url_when_titles_collide(tmp_path) -> None:
    """Two genuinely-different rows with the same title/platform but different
    source orders should NOT collapse — humble_url is the disambiguator."""
    old_csv = tmp_path / "old.csv"
    write_csv(
        [
            GameKey(
                game_title="Free Game",
                platform="steam",
                key="KEY-A",
                bundle_name="Bundle A",
                humble_url="https://www.humblebundle.com/downloads?key=AAA",
            )
        ],
        old_csv,
    )
    new_rows = [
        GameKey(
            game_title="Free Game",  # same title
            platform="steam",
            key="KEY-B",
            bundle_name="Bundle B",
            humble_url="https://www.humblebundle.com/downloads?key=BBB",  # different order
        ),
    ]
    merged = merge_with_existing(new_rows, old_csv)
    # Both should be preserved
    assert len(merged) == 2
    keys = {r.key for r in merged}
    assert keys == {"KEY-A", "KEY-B"}
