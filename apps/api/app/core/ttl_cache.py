"""A small bounded, expiring in-process cache. No I/O, no framework, one clock read per call.

In-process rather than Redis, for the same reason ``app.core.rate_limit`` is: this deployment is
one API process, adding a network hop to save a network hop is not an optimisation, and a cache
that cannot be reached when Redis is down is a cache that takes the route down with it. The cost
is that entries do not survive a restart and are not shared between processes — both of which are
fine for something whose worst case is doing the work again.

Two properties the callers depend on, and neither is decoration:

* **Bounded.** ``max_entries`` is enforced on every write by evicting the least recently used
  entry. Anything cached here is derived from patient data, so an unbounded dict is a process
  that holds every chart it has ever been asked about until it is restarted — a memory leak and
  a data-retention question at the same time.
* **Expiring.** An entry older than its TTL is not returned and is dropped when it is found. The
  TTL is a ceiling on how stale a *correct* answer may be; it is emphatically not the correctness
  mechanism. A caller whose key does not change when its inputs change has a bug that no TTL
  fixes, it only shortens.

The clock is ``time.monotonic``: this measures elapsed time, and a wall clock can go backwards.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expiries: int = 0


class TTLCache[V]:
    """Least-recently-used, time-limited, keyed by an opaque string."""

    def __init__(self, *, max_entries: int, ttl_seconds: float) -> None:
        self._entries: OrderedDict[str, tuple[float, V]] = OrderedDict()
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self.stats = CacheStats()

    @property
    def enabled(self) -> bool:
        """False when either bound is zero or negative — the switch, and it is checked on read
        *and* write so turning it off empties nothing but stores nothing either."""
        return self._max_entries > 0 and self._ttl_seconds > 0

    def get(self, key: str) -> V | None:
        if not self.enabled:
            return None
        found = self._entries.get(key)
        if found is None:
            self.stats.misses += 1
            return None
        stored_at, value = found
        if time.monotonic() - stored_at > self._ttl_seconds:
            # Dropped rather than left to be evicted later: an expired entry holding patient-
            # derived data is exactly what should not linger, and this is the moment we know.
            del self._entries[key]
            self.stats.expiries += 1
            self.stats.misses += 1
            return None
        self._entries.move_to_end(key)
        self.stats.hits += 1
        return value

    def put(self, key: str, value: V) -> None:
        if not self.enabled:
            return
        self._entries[key] = (time.monotonic(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self.stats.evictions += 1

    def clear(self) -> None:
        """Empty it. Used by tests, and by anything that needs to be sure nothing is held."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
