"""A per-provider circuit breaker for the outbound LLM calls.

Every provider SDK call in this codebase is already bounded by
``settings.llm_request_timeout_seconds``. That bounds one socket, and then the retry loop and
the fallback chain multiply it: ``LLMClient.complete_json`` tries each configured provider
``retries + 1`` times, so a provider that accepts a connection and then stops answering costs
90 seconds on the defaults, and a dead chain of three costs 270 — per agent call, of which one
reasoning run makes seven in sequence plus a parallel panel. See the arithmetic spelled out in
the ``reasoning_run_lease_minutes`` comment in ``app.config``.

The part a timeout cannot fix is that the cost is paid again on the next call. Nothing recorded
that the provider had just failed nine consecutive times, so every agent of every run
rediscovered the outage from scratch. This module is that memory.

**What counts as a failure.** Only failures that say something about the *provider*: timeouts,
connection errors, 429s, 5xx, and authentication rejections (a revoked key fails every call, so
it is an availability fact). A malformed or unparseable model response is not one — the provider
answered, the answer was bad, and taking a working provider out of service over one bad
completion would turn a prompt-shaped problem into an outage. See :func:`is_availability_failure`.

**States.** Closed (calls flow), open (calls are refused without a socket), half-open (one trial
call is admitted to find out whether the provider is back). Half-open admits exactly one caller;
the rest are refused until it reports back, because the concurrency this runs under is a
four-specialist hypothesis panel — letting all four probe a dead provider would reinstate the
very stall the breaker exists to remove.

**Fail-open on the safety side.** A refused provider is a refused *LLM* call, which degrades
reasoning to the deterministic path and marks the run degraded. It never touches the
deterministic allergy, contraindication and interaction checks, which do not call this module at
all (Critical Safety Rule #8). Degrading faster is the whole benefit: the clinician reaches the
offline answer while the question is still live.

State is process-local and deliberately so. It is an optimisation over observed failures, not a
distributed agreement — two workers each learning the outage once is a cost of two calls, and a
shared store would put a network dependency in the path that exists to survive network failures.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# Upstream statuses that mean "this provider cannot serve requests right now", as opposed to
# "this request was wrong". 429 and 5xx are the plain cases. 408 is a server-side read timeout.
# 401/403 are here because a rejected key rejects every call: it is indistinguishable from an
# outage from the caller's side, and retrying it 9 times per agent call is pure latency.
_AVAILABILITY_STATUSES = frozenset({401, 403, 408, 409, 425, 429})

# Exception *type name* fragments that mean a transport failure when no HTTP status came back —
# there is no response to read a code off when the connection never completed. Matched on the
# type name rather than on isinstance, because the SDKs are imported lazily (and optionally):
# importing openai and anthropic here to name their exception classes would undo that.
#
# Deliberately no "unavailable" fragment, though it reads like the most obvious one. The SDKs'
# service-unavailable errors all carry a 503 and are caught by the status branch above, while
# the name matches ``LLMUnavailable`` — this codebase's own wrapper, which ``_extract_json``
# raises for a reply with no JSON object in it. That is the commonest non-availability failure
# there is, and matching it here opened the breaker on providers that were answering fine.
_TRANSPORT_NAME_FRAGMENTS = ("timeout", "connection", "connect", "socket")


def is_availability_failure(exc: BaseException | None) -> bool:
    """Whether ``exc`` is evidence the provider itself is unavailable.

    The distinction the breaker turns on. A provider that answered with something unusable is
    up; a provider that never answered, or answered 503, is not. Getting this wrong in the
    permissive direction is the expensive mistake: it opens the breaker on a healthy provider
    because one prompt produced JSON we could not parse, and then every *other* patient's run
    degrades for the length of the cooldown.
    """
    if exc is None:
        return False

    status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        # A status came back, so the provider is answering. Only the ones that mean "not now".
        return status >= 500 or status in _AVAILABILITY_STATUSES

    name = type(exc).__name__.lower()
    return any(fragment in name for fragment in _TRANSPORT_NAME_FRAGMENTS)


@dataclass
class _ProviderState:
    consecutive_failures: int = 0
    opened_at: float | None = None
    # Set while a half-open trial call is in flight, so only one caller probes at a time.
    trial_in_flight: bool = False
    # Cumulative, for the operator view. Never reset by ``record_success``.
    opened_count: int = 0
    last_error: str | None = None


@dataclass
class ProviderCircuit:
    """Breaker state for every provider, guarded by one lock.

    One lock rather than one per provider: the critical sections are a handful of integer
    comparisons, and the callers are worker threads from a pool of eight.
    """

    _states: dict[str, _ProviderState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _state(self, provider: str) -> _ProviderState:
        state = self._states.get(provider)
        if state is None:
            state = _ProviderState()
            self._states[provider] = state
        return state

    def allow(self, provider: str, *, now: float | None = None) -> bool:
        """Whether a call to ``provider`` may be attempted right now.

        Returns True in the closed state, False while the breaker is open, and True for exactly
        one caller once the cooldown has elapsed — that caller owns the half-open trial and must
        report back through :meth:`record_success` or :meth:`record_failure`. A caller that
        neither succeeds nor fails (an exception escaping between the two) would leave the trial
        flag set and the provider skipped until the next cooldown, which is the safe direction:
        it errs towards not calling a provider that just misbehaved.
        """
        if not settings.llm_circuit_breaker_enabled:
            return True
        clock = time.monotonic() if now is None else now
        with self._lock:
            state = self._state(provider)
            if state.opened_at is None:
                return True
            if clock - state.opened_at < settings.llm_circuit_reset_seconds:
                return False
            if state.trial_in_flight:
                return False
            state.trial_in_flight = True
            logger.info("LLM provider %r breaker half-open: admitting one trial call", provider)
            return True

    def record_success(self, provider: str) -> None:
        """Report that a call to ``provider`` returned. Closes the breaker."""
        with self._lock:
            state = self._state(provider)
            was_open = state.opened_at is not None
            state.consecutive_failures = 0
            state.opened_at = None
            state.trial_in_flight = False
            state.last_error = None
        if was_open:
            logger.warning(
                "LLM provider %r breaker closed: the provider is answering again", provider
            )

    def record_failure(self, provider: str, *, reason: str, now: float | None = None) -> None:
        """Report an availability failure. Opens (or re-opens) the breaker at the threshold.

        ``reason`` must already be log-safe — callers pass it through
        :func:`~app.core.logsafe.describe_exception`, because it is stored and logged and a
        provider's own error text can quote the prompt back, and the prompt is the chart.
        """
        clock = time.monotonic() if now is None else now
        with self._lock:
            state = self._state(provider)
            was_trial = state.trial_in_flight
            state.trial_in_flight = False
            state.consecutive_failures += 1
            state.last_error = reason
            # A failed half-open trial re-opens immediately, whatever the counter says: the
            # cooldown just elapsed and the provider is still down, so restarting the clock is
            # the answer rather than admitting another trial on the next call.
            at_threshold = state.consecutive_failures >= settings.llm_circuit_failure_threshold
            tripped = was_trial or at_threshold
            if not tripped:
                return
            reopening = state.opened_at is not None or was_trial
            state.opened_at = clock
            state.opened_count += 1
            failures = state.consecutive_failures
        logger.warning(
            "LLM provider %r breaker open for %.0fs after %d consecutive availability "
            "failures (%s); calls to it are skipped until then%s",
            provider,
            settings.llm_circuit_reset_seconds,
            failures,
            reason,
            " (trial call failed)" if reopening else "",
        )

    def is_open(self, provider: str, *, now: float | None = None) -> bool:
        """Whether ``provider`` is currently being skipped. Does not claim a half-open trial.

        Read-only, unlike :meth:`allow` — for the health endpoint and for the "is anything left
        to try" check, neither of which should consume the one trial slot a cooldown opens.
        """
        if not settings.llm_circuit_breaker_enabled:
            return False
        clock = time.monotonic() if now is None else now
        with self._lock:
            state = self._states.get(provider)
            if state is None or state.opened_at is None:
                return False
            return clock - state.opened_at < settings.llm_circuit_reset_seconds

    def snapshot(self, *, now: float | None = None) -> dict[str, dict[str, Any]]:
        """Per-provider breaker state for ``GET /health/dependencies``.

        Only providers that have failed at least once appear. A provider absent from this map
        has never tripped, which is the state an operator does not need a row for.
        """
        clock = time.monotonic() if now is None else now
        report: dict[str, dict[str, Any]] = {}
        with self._lock:
            for provider, state in self._states.items():
                if state.opened_at is None and not state.consecutive_failures:
                    continue
                open_now = (
                    state.opened_at is not None
                    and clock - state.opened_at < settings.llm_circuit_reset_seconds
                )
                report[provider] = {
                    "state": "open" if open_now else "closed",
                    "consecutive_failures": state.consecutive_failures,
                    "opened_count": state.opened_count,
                    "seconds_until_retry": (
                        round(settings.llm_circuit_reset_seconds - (clock - state.opened_at), 1)
                        if open_now and state.opened_at is not None
                        else None
                    ),
                    "last_error": state.last_error,
                }
        return report

    def reset(self) -> None:
        """Forget every provider's state. For tests, and for nothing else."""
        with self._lock:
            self._states.clear()


# Process-wide, shared by the reasoning client and the extraction client on purpose: they call
# the same three providers over the same sockets, so an outage one of them discovers is one the
# other should not have to rediscover. A document upload arriving during a provider outage is
# exactly the request that should not spend 90 seconds finding out.
breaker = ProviderCircuit()
