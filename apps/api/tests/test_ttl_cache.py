"""The bounded expiring cache, on its own.

Its two guarantees are the reason anything derived from patient data is allowed to sit in it, so
they are asserted here rather than inferred from a caller's behaviour: it never grows past
``max_entries``, and it never returns an entry older than its TTL. What it is emphatically *not*
is a correctness mechanism — a caller whose key does not change when its inputs change has a bug
the TTL only shortens. See ``test_clinical_summary_cache`` for the key that does the real work.
"""

from __future__ import annotations

import pytest

from app.core.ttl_cache import TTLCache


def test_a_stored_value_comes_back():
    cache: TTLCache[str] = TTLCache(max_entries=4, ttl_seconds=60)
    cache.put("k", "v")

    assert cache.get("k") == "v"
    assert cache.stats.hits == 1


def test_an_absent_key_is_a_miss_and_not_an_error():
    cache: TTLCache[str] = TTLCache(max_entries=4, ttl_seconds=60)

    assert cache.get("nothing") is None
    assert cache.stats.misses == 1


def test_an_expired_entry_is_dropped_rather_than_returned(monkeypatch):
    """Dropped at the moment it is found, not left for a later eviction: an expired entry
    holding patient-derived data is exactly what should not linger, and this is the moment we
    know it is one."""
    # One read on `put`, one on `get`.
    clock = iter([0.0, 61.0])
    monkeypatch.setattr("app.core.ttl_cache.time.monotonic", lambda: next(clock))
    cache: TTLCache[str] = TTLCache(max_entries=4, ttl_seconds=60)
    cache.put("k", "v")

    assert cache.get("k") is None
    assert len(cache) == 0
    assert cache.stats.expiries == 1


def test_an_entry_inside_the_ttl_survives(monkeypatch):
    clock = iter([0.0, 59.0])
    monkeypatch.setattr("app.core.ttl_cache.time.monotonic", lambda: next(clock))
    cache: TTLCache[str] = TTLCache(max_entries=4, ttl_seconds=60)
    cache.put("k", "v")

    assert cache.get("k") == "v"


def test_it_never_grows_past_its_ceiling():
    """An unbounded dict here is a process that holds every chart it has ever been asked about
    until it is restarted — a memory leak and a data-retention question at once."""
    cache: TTLCache[int] = TTLCache(max_entries=3, ttl_seconds=60)

    for i in range(10):
        cache.put(f"k{i}", i)

    assert len(cache) == 3
    assert cache.stats.evictions == 7


def test_the_least_recently_used_entry_is_the_one_evicted():
    cache: TTLCache[int] = TTLCache(max_entries=2, ttl_seconds=60)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.get("a")  # `a` is now the most recently used, so `b` is next out.

    cache.put("c", 3)

    assert cache.get("a") == 1
    assert cache.get("b") is None
    assert cache.get("c") == 3


@pytest.mark.parametrize(("max_entries", "ttl"), [(0, 60), (4, 0), (0, 0), (-1, 60)])
def test_either_bound_at_zero_switches_it_off(max_entries, ttl):
    """Checked on write as well as on read, so switching it off stores nothing rather than
    quietly accumulating entries nobody will ever be given."""
    cache: TTLCache[str] = TTLCache(max_entries=max_entries, ttl_seconds=ttl)
    cache.put("k", "v")

    assert cache.enabled is False
    assert cache.get("k") is None
    assert len(cache) == 0


def test_clear_empties_it():
    cache: TTLCache[str] = TTLCache(max_entries=4, ttl_seconds=60)
    cache.put("k", "v")

    cache.clear()

    assert len(cache) == 0
    assert cache.get("k") is None
