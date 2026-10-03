"""CSV export for Humble key extractions."""

from __future__ import annotations

import csv
import logging
import re
from collections.abc import Iterable
from pathlib import Path

from humble_bundle_keys.models import CSV_HEADERS, GameKey

log = logging.getLogger(__name__)

# A bundle name like "December 2025 Humble Choice" — pull the date out.
_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b",
    re.IGNORECASE,
)


def _enrich(rows: Iterable[GameKey]) -> list[GameKey]:
    """Best-effort enrichment: extract bundle_date from bundle_name when missing."""
    out = []
    for row in rows:
        if not row.bundle_date and row.bundle_name:
            m = _DATE_RE.search(row.bundle_name)
            if m:
                month = m.group(1).capitalize()
                year = m.group(2)
                row.bundle_date = f"{month} {year}"
        out.append(row)
    return out


def write_csv(rows: Iterable[GameKey], output_path: Path) -> int:
    """Write rows to ``output_path``. Returns count written."""
    rows = _enrich(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        n = 0
        for row in rows:
            writer.writerow(row.to_row())
            n += 1
    log.info("Wrote %d rows to %s", n, output_path)
    return n


def _identity(gk: GameKey) -> tuple[str, str, str]:
    """Stable identity for a row across runs.

    Uses ``humble_url`` (which carries the order's gamekey URL parameter and
    is the most stable cross-run identifier) plus ``game_title`` and
    ``platform``. We deliberately do NOT include ``bundle_name`` because
    earlier versions of this tool extracted it from the wrong API field —
    so the same physical row could appear with different bundle_names
    across versions.

    Falls back to ``(game_title, bundle_name, platform)`` only when
    humble_url is empty (e.g. very old CSVs from a now-fixed bug).
    """
    if gk.humble_url:
        return ("url:" + gk.humble_url, gk.game_title.lower(), gk.platform.lower())
    return ("nameonly", gk.game_title.lower(), (gk.bundle_name + "|" + gk.platform).lower())


def merge_with_existing(
    new_rows: Iterable[GameKey],
    existing_path: Path,
) -> list[GameKey]:
    """If ``existing_path`` exists, merge so that revealed keys aren't lost.

    Identity is derived via :func:`_identity` (humble_url + title + platform).
    New rows override only when they carry strictly more information (key
    now revealed, deadline now populated, etc.).
    """
    new_rows = list(new_rows)
    if not existing_path.exists():
        return new_rows

    existing: dict[tuple[str, str, str], GameKey] = {}
    with existing_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            gk = GameKey(
                game_title=r.get("game_title", ""),
                platform=r.get("platform", ""),
                key=r.get("key", ""),
                bundle_name=r.get("bundle_name", ""),
                bundle_date=r.get("bundle_date", ""),
                redemption_deadline=r.get("redemption_deadline", ""),
                redeemed_on_humble=r.get("redeemed_on_humble", "false").lower() == "true",
                os_support=r.get("os_support", ""),
                humble_url=r.get("humble_url", ""),
                steam_app_id=int(r["steam_app_id"]) if r.get("steam_app_id", "").isdigit()
                else None,
            )
            existing[_identity(gk)] = gk

    merged_by_id: dict[tuple[str, str, str], GameKey] = dict(existing)
    for new in new_rows:
        key_id = _identity(new)
        prev = merged_by_id.get(key_id)
        if prev is None:
            merged_by_id[key_id] = new
            continue
        # Prefer the row with a non-empty key.
        if new.key and not prev.key:
            merged_by_id[key_id] = new
        elif prev.key and not new.key:
            # Keep prev but pull through any newly-discovered metadata.
            prev.redemption_deadline = new.redemption_deadline or prev.redemption_deadline
            prev.os_support = new.os_support or prev.os_support
            prev.humble_url = new.humble_url or prev.humble_url
            prev.steam_app_id = new.steam_app_id or prev.steam_app_id
            # Newer extractor reads bundle_name from the right field; if old
            # row had blank bundle_name and new has a real one, take it.
            if not prev.bundle_name and new.bundle_name:
                prev.bundle_name = new.bundle_name
            if not prev.bundle_date and new.bundle_date:
                prev.bundle_date = new.bundle_date
            merged_by_id[key_id] = prev
        else:
            merged_by_id[key_id] = new

    return list(merged_by_id.values())
