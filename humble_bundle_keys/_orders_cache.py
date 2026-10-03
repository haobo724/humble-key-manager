"""On-disk cache for /api/v1/order/<gamekey> responses.

Order data only changes when:
* the user buys a new bundle / subscription month (new gamekey appears)
* a key is revealed (``redeemed_key_val`` populates on a tpk)
* a Choice game is claimed (a tpk gets added to ``tpkd_dict.all_tpks``)

So caching individual order JSON for a few hours dramatically cuts run time
without staleness risk for the read-only metadata. The list of gamekeys
(``/api/v1/user/order``) is still fetched fresh every run — it's tiny and
shows new orders immediately.

Cache invalidation:
* TTL (default 6h) — silently re-fetch after expiry.
* Explicit invalidation on successful state-changing operations (e.g. after
  ``_reveal`` returns a key, the caller should call ``invalidate(gamekey)``
  so the next run picks up the new ``redeemed_key_val``).
* ``--no-cache`` flag bypasses entirely.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path.home() / ".humble-bundle-keys" / "orders-cache"
DEFAULT_TTL_S = 6 * 60 * 60  # 6 hours

log = logging.getLogger(__name__)


class OrderCache:
    """Tiny single-process JSON cache for order detail responses."""

    def __init__(
        self,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        ttl_s: int = DEFAULT_TTL_S,
        enabled: bool = True,
    ):
        self.cache_dir = cache_dir
        self.ttl_s = ttl_s
        self.enabled = enabled
        self._hits = 0
        self._misses = 0
        if enabled:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                log.warning("Could not create cache dir %s: %s", cache_dir, e)
                self.enabled = False

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def _path(self, gamekey: str) -> Path:
        # Sanitise to a safe filename. Gamekeys are alnum so this is mostly
        # defensive against future shape changes.
        safe = "".join(c if c.isalnum() else "_" for c in gamekey)
        return self.cache_dir / f"{safe}.json"

    def get(self, gamekey: str, *, count_stats: bool = True) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self._path(gamekey)
        if not path.exists():
            if count_stats:
                self._misses += 1
            return None
        try:
            age = time.time() - path.stat().st_mtime
        except Exception:
            if count_stats:
                self._misses += 1
            return None
        if self.ttl_s <= 0 or age >= self.ttl_s:
            if count_stats:
                self._misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.debug("Cache file %s unreadable (%s); treating as miss", path, e)
            if count_stats:
                self._misses += 1
            return None
        if count_stats:
            self._hits += 1
        return data

    def put(self, gamekey: str, order: dict[str, Any]) -> None:
        if not self.enabled:
            return
        path = self._path(gamekey)
        try:
            path.write_text(json.dumps(order), encoding="utf-8")
        except Exception as e:
            log.debug("Failed to write cache %s: %s", path, e)

    def invalidate(self, gamekey: str) -> None:
        """Drop a cached order — call after any state-changing operation."""
        if not self.enabled:
            return
        path = self._path(gamekey)
        try:
            if path.exists():
                path.unlink()
        except Exception as e:
            log.debug("Failed to invalidate cache %s: %s", path, e)

    def clear_all(self) -> int:
        """Remove every cached order. Returns count removed."""
        if not self.enabled or not self.cache_dir.exists():
            return 0
        n = 0
        for p in self.cache_dir.glob("*.json"):
            try:
                p.unlink()
                n += 1
            except Exception:
                pass
        return n
