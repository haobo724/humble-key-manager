"""Data models for the Humble keys extractor."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class GameKey:
    """A single game key entry.

    A row in the output CSV corresponds to one of these. A bundle that
    contains 12 games produces 12 GameKey rows.
    """

    game_title: str
    platform: str  # 'steam', 'gog', 'origin', 'uplay', 'epic', 'other', or '' if unknown
    key: str  # the actual redemption key, or '' if not yet revealed
    bundle_name: str = ""
    bundle_date: str = ""  # ISO date or original string
    redemption_deadline: str = ""  # e.g. "January 6, 2027 by 10:00 AM PST" — kept verbatim
    redeemed_on_humble: bool = False  # True if Humble shows the key as claimed
    os_support: str = ""  # comma-separated, e.g. "Windows, macOS"
    humble_url: str = ""  # link to the bundle/order page
    steam_app_id: int | None = None  # exact product metadata; never infer from a similar title
    exclusive_countries: list[str] | None = None
    disallowed_countries: list[str] | None = None

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["redeemed_on_humble"] = "true" if self.redeemed_on_humble else "false"
        # Keep the original CLI CSV format; the web export includes region metadata.
        d.pop("exclusive_countries")
        d.pop("disallowed_countries")
        return d


CSV_HEADERS = [
    "game_title",
    "platform",
    "key",
    "bundle_name",
    "bundle_date",
    "redemption_deadline",
    "redeemed_on_humble",
    "os_support",
    "humble_url",
    "steam_app_id",
]


@dataclass
class ExtractStats:
    """Summary statistics for a run."""

    total_rows: int = 0
    keys_revealed: int = 0  # newly revealed this run
    keys_already_revealed: int = 0
    bundles_processed: int = 0
    # Reveal calls that returned 2xx but didn't produce a key. Strong signal
    # that those games need the two-step Choice claim flow (--claim-choice)
    # rather than a direct redeemkey.
    keys_silent_no_response: int = 0
    # Per-tpk metadata for silent-no-response cases. Each entry is
    # (game_title, tpk_machine_name, order_machine_name). Useful for
    # diagnosing which keytype patterns aren't being recognised.
    silent_no_response_tpks: list[tuple[str, str, str]] = field(default_factory=list)
    # Entries pre-skipped because their keytype is structurally not
    # auto-redeemable (softwarebundle, voucher, keyless, freegame).
    skipped_structural: int = 0
    # Per-tpk metadata for skipped entries. Each entry is
    # (game_title, tpk_machine_name, category).
    skipped_structural_tpks: list[tuple[str, str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
