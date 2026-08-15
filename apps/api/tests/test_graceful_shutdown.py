"""Shutdown lets go of the LLM side instead of leaving the interpreter holding it.

Nothing ever shut the agents' thread pool down. That reads like a harmless omission — the
process is exiting anyway — and it is not, because CPython joins a ``ThreadPoolExecutor``'s
workers from an ``atexit`` hook: the interpreter does not exit until every in-flight call has
returned. The call this pool exists to isolate is a provider that accepted the connection and
stopped answering, so a deploy landing during a provider outage left the outgoing worker
holding a socket nothing was waiting on, until systemd's ``TimeoutStopSec`` ran out and
SIGKILLed it. ``deploy.sh`` restarts the service synchronously, so the deploy waited too.

What is fixable is the queued half, and that is what these pin: calls sitting behind a
saturated pool are dropped without ever opening a socket. A call already *running* is blocked
inside a synchronous SDK in a thread and Python cannot interrupt it — that one is bounded by
``llm_request_timeout_seconds`` and the circuit breaker, not by us, and the test below says so
rather than pretending otherwise.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import CancelledError

import pytest

from app.agents import llm, util
from app.config import settings


@pytest.fixture(autouse=True)
def _fresh_pool():
    util.reset_llm_executor()
    yield
    util.reset_llm_executor()


def test_queued_calls_are_cancelled_at_shutdown(monkeypatch):
    """Work that had not started when shutdown began never runs.

    The pool is deliberately narrow (``llm_max_concurrent_calls``), so during an outage the
    queue behind it is where most of a run's calls are. Letting those start on the way out
    would have a shutting-down worker opening fresh provider sockets.
    """
    monkeypatch.setattr(settings, "llm_max_concurrent_calls", 1)
    executor = util.llm_executor()

    holding = threading.Event()
    release = threading.Event()
    started: list[int] = []

    def occupy() -> None:
        holding.set()
        release.wait(timeout=10)

    def queued(n: int) -> None:
        started.append(n)

    busy = executor.submit(occupy)
    assert holding.wait(timeout=10), "the pool never picked up the first call"
    queued_futures = [executor.submit(queued, n) for n in range(5)]

    util.shutdown_llm_executor()
    release.set()
    busy.result(timeout=10)

    assert started == [], "a queued provider call started after shutdown had begun"
    for f in queued_futures:
        with pytest.raises(CancelledError):
            f.result(timeout=5)


def test_shutdown_does_not_block_on_a_running_call(monkeypatch):
    """Shutdown returns immediately rather than waiting out a hung provider.

    ``wait=False`` is the point. The running call is still holding its thread afterwards — that
    is asserted here too, so the limit of this fix is written down rather than assumed away.
    """
    monkeypatch.setattr(settings, "llm_max_concurrent_calls", 2)
    executor = util.llm_executor()

    holding = threading.Event()
    release = threading.Event()

    def hung_provider() -> str:
        holding.set()
        release.wait(timeout=30)
        return "eventually"

    future = executor.submit(hung_provider)
    assert holding.wait(timeout=10)

    started = time.monotonic()
    util.shutdown_llm_executor()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"shutdown blocked for {elapsed:.2f}s on a call it cannot interrupt"
    assert not future.done(), "the running call was expected to still be in flight"

    release.set()
    assert future.result(timeout=10) == "eventually"


def test_shutdown_is_safe_to_call_twice_and_rebuilds_after(monkeypatch):
    """Idempotent, and the pool comes back if anything still needs one.

    A lifespan that runs twice in one process is ordinary in tests, and a shutdown that then
    handed out a dead executor would fail the next call rather than the previous run.
    """
    monkeypatch.setattr(settings, "llm_max_concurrent_calls", 2)
    first = util.llm_executor()

    util.shutdown_llm_executor()
    util.shutdown_llm_executor()  # must not raise

    second = util.llm_executor()
    assert second is not first
    assert second.submit(lambda: 7).result(timeout=10) == 7


def test_shutdown_with_an_idle_pool_that_was_never_built():
    """Never having made an LLM call is not a shutdown failure."""
    util.shutdown_llm_executor()
    assert util._llm_executor is None
    util.shutdown_llm_executor()  # still fine


@pytest.mark.asyncio
async def test_the_lifespan_releases_the_llm_side_on_the_way_down(monkeypatch):
    """The app's own shutdown path calls both releases, in that order.

    Ordering matters: the executor is stopped first so no worker thread is part-way through a
    request on a client while that client is being closed.
    """
    from app.main import lifespan

    calls: list[str] = []
    monkeypatch.setattr("app.main.shutdown_llm_executor", lambda: calls.append("executor"))
    monkeypatch.setattr("app.main.close_provider_clients", lambda: calls.append("clients"))
    monkeypatch.setattr("app.main.assert_production_config", lambda: None)

    async def _no_seed() -> None:
        return None

    async def _no_dispose() -> None:
        calls.append("engine")

    monkeypatch.setattr("app.main._seed_drug_data", _no_seed)
    monkeypatch.setattr("app.main.dispose_engine", _no_dispose)

    async with lifespan(None):  # type: ignore[arg-type]
        assert calls == [], "shutdown work ran at startup"

    assert calls == ["executor", "clients", "engine"]


@pytest.mark.asyncio
async def test_the_lifespan_closes_the_real_provider_clients(monkeypatch):
    """End to end through the real functions: a cached client is closed by app shutdown."""
    from app.main import lifespan

    closed: list[str] = []

    class _Client:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.kwargs = kwargs

        def close(self) -> None:
            closed.append("closed")

    monkeypatch.setitem(__import__("sys").modules, "openai", type("M", (), {"OpenAI": _Client}))
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 30)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr("app.main.assert_production_config", lambda: None)

    async def _no_seed() -> None:
        return None

    async def _no_dispose() -> None:
        return None

    monkeypatch.setattr("app.main._seed_drug_data", _no_seed)
    monkeypatch.setattr("app.main.dispose_engine", _no_dispose)

    llm.close_provider_clients()
    async with lifespan(None):  # type: ignore[arg-type]
        llm._openai_compatible_client("sk-test", None, 30)
        assert len(llm._client_cache) == 1

    assert closed == ["closed"], "app shutdown left the provider connections open"
    assert not llm._client_cache
