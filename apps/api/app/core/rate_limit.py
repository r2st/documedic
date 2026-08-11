"""Deterministic in-process rate limiting for the expensive clinical endpoints.

Why this exists
---------------
Every route under ``/reasoning`` fans out to eight LLM agents, and a document upload runs
multimodal extraction over a scan. Both are billed per call against one shared upstream quota,
and neither had a ceiling of any kind. A single client in a retry loop — a Reasoning Theatre tab
whose ``EventSource`` reconnects on every error, a misconfigured integration re-POSTing a run —
could drain the provider budget for every clinician on the deployment. To everyone else that
presents as "AI reasoning paused — offline" with no cause and nothing to act on.

Why in-process, and not Redis
-----------------------------
Redis is in the dependency list but nothing in the API touches it, and the brute-force control
this sits beside (:meth:`app.services.auth_service.AuthService._assert_not_locked_out`)
deliberately counts from the append-only audit log so it needs no second datastore. A rate
limiter cannot borrow that trick: writing a row into an immutable, hash-chained, never-pruned
clinical audit table in order to decide whether to serve the request that would write it is
worse than the problem it solves. So this keeps its counters in memory.

The consequences are bounded and both fail in the forgiving direction:

* **The limit is per process.** The Dockerfile runs one uvicorn worker, so per-process is
  currently per-deployment. Growing that to ``--workers N`` multiplies the effective ceiling by
  N — a looser limit, never an absent one.
* **A restart forgets every window.** That forgives in-flight callers rather than locking
  anyone out.

What is deliberately *not* limited
----------------------------------
The deterministic drug-safety routes (allergy cross-checks, interaction and contraindication
detection) and the critical-lab check are never metered. They are local, rule-based and cheap —
there is no upstream spend to protect — and a 429 in that path is actively dangerous: a
clinician who asked "does this conflict with anything?" and got an error back mid-consultation
reads the absence of a hard block as the absence of a conflict. Those checks must answer
whenever they are asked. The same reasoning keeps chart reads unmetered.
"""

from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Ceiling on distinct keys held at once. Each key costs an OrderedDict entry plus at most
# `max_requests` floats, so 10k keys is well under a megabyte — sized to be unreachable by the
# real population (one key per clinician account per bucket) rather than to be tuned.
MAX_TRACKED_KEYS = 10_000


@dataclass(frozen=True)
class RateLimit:
    """A ceiling of ``max_requests`` inside a rolling window of ``window_seconds``."""

    max_requests: int
    window_seconds: float

    def __post_init__(self) -> None:
        if self.max_requests < 1:
            raise ValueError(
                "max_requests must be >= 1; express 'no limit' by not constructing a RateLimit "
                "(see app.dependencies.limit_for, where a non-positive setting disables the "
                "bucket) rather than by a limit of zero, which would reject every request."
            )
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")


class SlidingWindowLimiter:
    """Exact sliding-window request counter keyed by an opaque string.

    A window *log* rather than a fixed-bucket counter: it keeps the timestamp of each hit, so
    the limit holds across bucket boundaries. A fixed 60-second bucket would let a caller spend
    its whole minute's quota at 11:59:59 and the next one at 12:00:00 — double the intended
    rate at exactly the moment a retry storm produces. The extra cost is one float per hit
    inside the window, bounded by ``max_requests``.

    Not thread-safe, and does not need to be: FastAPI dependencies run on the single asyncio
    event loop, and :meth:`check` contains no await point, so no two calls interleave.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_tracked_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        # Monotonic by default: a wall-clock jump (NTP correction, DST) would otherwise either
        # expire every window at once or park them in the future.
        self._clock = clock
        self._max_tracked_keys = max_tracked_keys
        # Insertion-ordered so eviction can take the least-recently-touched key.
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def check(self, key: str, limit: RateLimit) -> float | None:
        """Count a request against ``key``, or report how long it must wait.

        Returns ``None`` when the request is within ``limit`` — and counts it. Otherwise returns
        the seconds until the oldest hit in the window ages out, and counts **nothing**: a
        rejected request that recorded itself would push its own retry further away every time,
        so a client polling at the limit would lock itself out indefinitely instead of settling
        into the allowed rate.
        """
        now = self._clock()
        hits = self._hits.get(key)
        if hits is None:
            hits = deque()
            self._hits[key] = hits
        else:
            self._hits.move_to_end(key)

        cutoff = now - limit.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= limit.max_requests:
            # Clamp: the subtraction can land marginally negative on a coarse clock, and a
            # negative wait would serialize as a Retry-After of 0 ("try again now").
            return max(hits[0] + limit.window_seconds - now, 0.0)

        hits.append(now)
        self._evict_if_needed()
        return None

    def _evict_if_needed(self) -> None:
        """Hold the key table at its cap, discarding the least recently seen keys first.

        Least-recently-touched is also most-likely-already-expired, so the common case forgets
        keys that were about to be pruned anyway. Evicting one that still had live hits raises
        that caller's ceiling for one window, which is why it is logged: it means the cap is
        being reached, and a limit that quietly stops limiting is the failure worth seeing.
        """
        while len(self._hits) > self._max_tracked_keys:
            key, hits = self._hits.popitem(last=False)
            if hits:
                logger.warning(
                    "Rate-limit table hit its %d-key cap; dropped the live window for %r, "
                    "which lifts that caller's ceiling for one window.",
                    self._max_tracked_keys,
                    key,
                )

    def reset(self) -> None:
        """Forget every window. For tests, and for nothing else."""
        self._hits.clear()

    @property
    def tracked_keys(self) -> int:
        """How many keys currently hold state — for tests and diagnostics."""
        return len(self._hits)


def retry_after_seconds(wait: float) -> int:
    """Round a wait up to the whole seconds an HTTP ``Retry-After`` header can carry.

    Rounding *up* matters: RFC 9110 allows only an integer, and truncating a 0.4-second wait to
    ``Retry-After: 0`` invites the client to retry immediately into another 429.
    """
    return max(int(math.ceil(wait)), 1)
