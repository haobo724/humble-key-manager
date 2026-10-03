"""Tests for cache hit/miss counter accuracy.

Covers bug #3: cache counters should add up to the number of primary order
fetches, not be inflated by internal re-fetches (e.g. the reveal fallback
path that re-reads an order after invalidation).
"""

from __future__ import annotations

from pathlib import Path

from humble_bundle_keys._orders_cache import OrderCache


def test_cache_hit_miss_sum_equals_primary_lookups(tmp_path: Path) -> None:
    """hits + misses should equal the number of count_stats=True calls."""
    cache = OrderCache(cache_dir=tmp_path, ttl_s=3600, enabled=True)

    # Simulate 5 orders: 2 cached, 3 fresh
    for gk in ("aaa", "bbb"):
        cache.put(gk, {"gamekey": gk, "data": "cached"})

    # Primary lookups (count_stats=True, the default)
    for gk in ("aaa", "bbb", "ccc", "ddd", "eee"):
        cache.get(gk)

    assert cache.hits == 2
    assert cache.misses == 3
    assert cache.hits + cache.misses == 5  # equals total primary lookups


def test_internal_refetch_does_not_inflate_counters(tmp_path: Path) -> None:
    """Calls with count_stats=False should not affect hit/miss counters."""
    cache = OrderCache(cache_dir=tmp_path, ttl_s=3600, enabled=True)
    cache.put("order1", {"gamekey": "order1"})

    # Primary lookup
    cache.get("order1")
    assert cache.hits == 1
    assert cache.misses == 0

    # Simulate reveal invalidation + re-fetch (internal, count_stats=False)
    cache.invalidate("order1")
    result = cache.get("order1", count_stats=False)
    assert result is None  # invalidated, so miss

    # Counters should NOT have changed
    assert cache.hits == 1
    assert cache.misses == 0


def test_expired_cache_counts_as_miss(tmp_path: Path) -> None:
    """An expired entry should count as a miss in primary lookups."""
    cache = OrderCache(cache_dir=tmp_path, ttl_s=0, enabled=True)  # 0s TTL = always expired
    cache.put("order1", {"gamekey": "order1"})

    # Even though the file exists, TTL=0 means it's expired
    result = cache.get("order1")
    assert result is None
    assert cache.hits == 0
    assert cache.misses == 1


def test_disabled_cache_does_not_count(tmp_path: Path) -> None:
    """A disabled cache should not increment any counters."""
    cache = OrderCache(cache_dir=tmp_path, ttl_s=3600, enabled=False)
    result = cache.get("anything")
    assert result is None
    assert cache.hits == 0
    assert cache.misses == 0
