"""What a hanging LLM provider costs, and what bounds it.

A provider that refuses promptly was always handled: the client fails over, the agents take their
deterministic path, the case comes back marked ``degraded``. A provider that *accepts the
connection and then stops answering* is a different failure, and two things were unbounded under
it.

**The blast radius.** The provider SDKs are synchronous, so each call leaves the event loop on a
worker thread — and ``asyncio.to_thread`` puts it on the loop's *default* executor, shared with
every other blocking thing in the process: bcrypt behind ``verify_password``, document blob I/O,
upload hashing. That pool is ``min(32, cpu_count + 4)`` threads, six on the box this deploys to,
and one run's hypothesis panel takes four of them at once under ``asyncio.gather``. Two
clinicians running the panel during such an outage filled it, and everything else queued behind
them — logging in stopped working because a third party's socket was hanging. LLM calls now run
on their own bounded pool, so saturation is contained: reasoning queues, nothing else notices.

**The duration.** ``llm_request_timeout_seconds`` bounds one socket and
``reasoning_run_lease_minutes`` bounds one claim; neither stops a run *making more calls*. One
``complete_json`` against a hanging upstream costs that timeout times (retries + 1) times every
provider in the chain — 4.5 minutes on the shipped defaults — and ``run_reasoning`` makes seven
of them in sequence plus the panel. Past half an hour, with the clinician watching a Reasoning
Theatre that had stopped emitting, and the run by then outliving the lease it published under.
A run now carries a wall-clock budget: once spent, no further call is started and the remaining
nodes take the offline path, so the case comes back degraded and escalated in bounded time.

Both are about *bounding* an outage, not about output. Rule #1 still applies either way — the
Verifier runs on the deterministic path exactly as it does on the LLM one.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time

import pytest

from app.agents.context import ReasoningContext
from app.agents.util import call_llm, llm_executor, reset_llm_executor
from app.config import settings


class _HangingClient:
    """A provider that accepts the call and never answers, like a black-holed socket."""

    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.calls = 0

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str) -> dict:
        self.calls += 1
        self.release.wait(timeout=10)
        return {"answered": True}


class _InstantClient:
    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str) -> dict:
        self.calls += 1
        return {"answered": True}


def _ctx(client, deadline: float | None = None) -> ReasoningContext:
    return ReasoningContext(llm=client, verifier_llm=client, deadline=deadline)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _fresh_pool():
    """Each test gets a pool built at that test's configured size, and leaves none behind."""
    reset_llm_executor()
    yield
    reset_llm_executor()


# ------------------------------------------------------------------ blast radius


async def test_hung_llm_calls_do_not_run_on_the_shared_default_executor():
    """The regression itself: a saturating LLM outage must leave unrelated work unaffected.

    The default executor is pinned small here, as it is on a 2-vCPU host, and then filled with
    more hung LLM calls than it has threads. ``verify_password`` is stood in for by any
    ``to_thread`` call — what is being asserted is that the two pools are not the same pool.
    """
    loop = asyncio.get_running_loop()
    previous = getattr(loop, "_default_executor", None)
    pinned = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(pinned)

    release = threading.Event()
    client = _HangingClient(release)
    ctx = _ctx(client)

    hung = [asyncio.create_task(call_llm(ctx, "sys", "user")) for _ in range(4)]
    try:
        # Waited for from the event loop, never through ``to_thread``: a helper that needs a
        # default-executor thread to observe that the default executor is starved cannot report
        # starvation, it can only join it. That is what made an earlier version of this test pass
        # against the very bug it describes — it blocked until the hung calls timed out, then
        # asserted against a pool that had drained in the meantime.
        for _ in range(50):
            if client.calls == 4:
                break
            await asyncio.sleep(0.02)
        assert client.calls == 4, (
            f"only {client.calls}/4 LLM calls got a thread; they are competing for the "
            "two-thread default executor instead of running on their own pool"
        )

        # And the default executor still has both of its threads free.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "login ok"), timeout=2.0) == (
            "login ok"
        )
    finally:
        release.set()
        await asyncio.gather(*hung)
        # Only if there was one: the loop refuses ``None``, and it has no default executor at
        # all until something asks for one. Other tests on a shared loop must not inherit the
        # two-thread pin, so the substitute is shut down either way.
        if isinstance(previous, concurrent.futures.ThreadPoolExecutor):
            loop.set_default_executor(previous)
        else:
            loop._default_executor = None  # type: ignore[attr-defined]
        pinned.shutdown(wait=False)


async def test_the_llm_pool_is_bounded_so_an_outage_cannot_grow_threads_without_limit(
    monkeypatch,
):
    """Saturation is contained rather than unbounded: extra calls queue for a slot."""
    monkeypatch.setattr(settings, "llm_max_concurrent_calls", 2)
    reset_llm_executor()
    assert llm_executor()._max_workers == 2

    release = threading.Event()
    client = _HangingClient(release)
    ctx = _ctx(client)

    calls = [asyncio.create_task(call_llm(ctx, "sys", "user")) for _ in range(4)]
    try:
        for _ in range(50):
            if client.calls >= 2:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        # Two are in flight; the other two are waiting for a thread, not spawning one.
        assert client.calls == 2
    finally:
        release.set()
        await asyncio.gather(*calls)


async def test_a_pool_size_of_zero_still_yields_a_usable_pool(monkeypatch):
    """A misconfiguration must not make every LLM call raise on pool construction."""
    monkeypatch.setattr(settings, "llm_max_concurrent_calls", 0)
    reset_llm_executor()
    assert llm_executor()._max_workers == 1
    assert await call_llm(_ctx(_InstantClient()), "sys", "user") == {"answered": True}


# ------------------------------------------------------------------ run budget


async def test_a_call_is_not_started_once_the_run_is_out_of_budget():
    client = _InstantClient()
    ctx = _ctx(client, deadline=time.monotonic() - 1)

    assert await call_llm(ctx, "sys", "user") is None
    assert client.calls == 0, "an expired budget must stop the call, not just discard its result"


async def test_the_verifier_is_held_to_the_same_budget():
    """Rule #1 is that the Verifier always runs, not that it always runs on an LLM.

    It has its own client and its own availability question, so it needs its own assertion that
    the budget reaches it — otherwise the one agent that cannot be skipped would be the one that
    could still hang for another 4.5 minutes after every other node had given up.
    """
    client = _InstantClient()
    ctx = _ctx(client, deadline=time.monotonic() - 1)

    assert await call_llm(ctx, "sys", "user", verifier=True) is None
    assert client.calls == 0


async def test_a_run_inside_its_budget_calls_the_provider_normally():
    client = _InstantClient()
    ctx = _ctx(client, deadline=time.monotonic() + 300)

    assert await call_llm(ctx, "sys", "user") == {"answered": True}
    assert client.calls == 1


async def test_a_context_with_no_deadline_is_unbounded():
    """Agent unit tests and any direct construction get no budget — it is a property of a run."""
    ctx = _ctx(_InstantClient())
    assert ctx.deadline is None
    assert ctx.out_of_budget() is False
    assert await call_llm(ctx, "sys", "user") == {"answered": True}


async def test_the_budget_reports_spent_only_once_the_deadline_has_passed():
    ctx = _ctx(_InstantClient(), deadline=time.monotonic() + 60)
    assert ctx.out_of_budget() is False
    ctx.deadline = time.monotonic()
    assert ctx.out_of_budget() is True


async def test_a_run_out_of_budget_still_completes_degraded_and_escalated():
    """End to end: the budget must degrade the run, not fail it or quietly weaken it.

    The whole point of stopping the calls is that the clinician gets an answer. So the pipeline
    has to reach the end, the Verifier has to run (Rule #1 does not have an outage exemption),
    the case has to come back marked ``degraded`` — and the conservative direction has to win, so
    a case assembled entirely from the deterministic floor is the one that most demands the
    clinician's attention rather than the one that reads as unremarkable.
    """
    from app.agents import graph
    from app.agents.state import CaseState

    client = _InstantClient()
    ctx = _ctx(client, deadline=time.monotonic() - 1)
    state = CaseState(
        patient_id="p1",
        presenting_complaint="Crushing central chest pain radiating to the jaw, 40 minutes.",
        patient_graph_snapshot={},
    )

    output = await graph.run_reasoning(state, ctx)

    assert client.calls == 0, "an exhausted budget must reach every node, not just the first"
    assert state.degraded is True
    assert state.autonomy_tier == "flag_for_review"
    assert state.verifier_status, "the Verifier still ran and recorded a status"
    assert "suggestions" in output


def test_the_budget_leaves_room_for_one_worst_case_call_inside_the_lease():
    """The inequality the budget was chosen against, pinned so a later edit cannot break it.

    The check happens before a call rather than interrupting one — a synchronous SDK call on a
    worker thread cannot be cancelled — so a run's real ceiling is the budget plus one full
    retry-and-failover ladder. That total has to stay inside the lease, or a run can still be
    taken over while it is publishing, which is the thing ``_finish_claimed_run`` then has to
    throw its output away over.
    """
    worst_case_call = (
        settings.llm_request_timeout_seconds
        * 3  # retries + 1, the default in LLMClient.complete_json
        * 3  # providers in the fallback chain
    )
    ceiling = settings.reasoning_llm_budget_seconds + worst_case_call
    assert ceiling < settings.reasoning_run_lease_minutes * 60, (
        f"a run can reach {ceiling}s but the claim lease is only "
        f"{settings.reasoning_run_lease_minutes * 60}s"
    )
