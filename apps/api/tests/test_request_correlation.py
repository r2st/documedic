"""One request, one id, on every log line the request produces — wherever it produces it.

The correlation id has existed for a long time, on ``request.state``. That reaches exactly the
code holding a ``Request``, which is three exception handlers in ``app.main``; they interpolated
it into their own messages by hand. Every other log line in the process — the safety service,
the eight agent nodes, the extraction pipeline, the storage layer, the SSE worker — carried no
id at all. So the reference a clinician reads off a 500 page found the handler line that
produced the 500, and nothing about what actually went wrong leading up to it.

``agents.util.call_llm`` has been copying the context across to the LLM thread pool since that
pool was introduced, with a comment naming "the request id the logs are correlated by". There
was no such variable. This module is about the variable existing and about it reaching all four
places the request's work spreads out to:

  * the same coroutine (services, agents),
  * a child task (``asyncio.create_task`` — the SSE worker),
  * a default-pool thread (``asyncio.to_thread`` — blob I/O, bcrypt, the embedder),
  * a dedicated-pool thread (``run_in_executor`` — the LLM calls and the provider probe),

the last of which does *not* propagate context on its own and is why the copy is written out.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import APIRouter

from app.core.logging_config import configure_logging
from app.core.request_context import (
    bind_request_id,
    current_request_id,
    reset_request_id,
)

logger = logging.getLogger("app.tests.correlation")


@pytest.fixture
def correlated_records():
    """Every record emitted under ``app.*``, with the filter applied, as it would be written.

    The filter is what the assertion is about: it is attached to the handler in production, so
    a test reading raw records would be checking a property nothing depends on.
    """
    from app.core.logging_config import RequestIdFilter

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger("app")
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def _ids(records: list[logging.LogRecord], needle: str) -> list[str]:
    return [getattr(r, "request_id", None) for r in records if needle in r.getMessage()]  # type: ignore[misc]


# --- The plain in-request case --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_service_log_line_carries_the_request_id(app, client, correlated_records):
    """A log line emitted well below the router — where every interesting one is — is filed
    under the same id the caller got back in ``X-Request-Id``."""
    router = APIRouter()

    @router.get("/_corr/deep")
    async def deep() -> dict:
        # Stands in for SafetyService, an agent node, the extraction pipeline: code that has
        # no Request object and never did.
        logger.info("deep-marker reached")
        return {"ok": True}

    app.include_router(router)

    resp = await client.get("/_corr/deep")
    assert resp.status_code == 200
    assert _ids(correlated_records, "deep-marker") == [resp.headers["X-Request-Id"]]


@pytest.mark.asyncio
async def test_the_clients_own_id_is_the_one_on_the_records(app, client, correlated_records):
    """Correlation across a proxy and the frontend is the whole reason an inbound id is
    adopted; adopting it only for the response header would be half the feature."""
    router = APIRouter()

    @router.get("/_corr/echo")
    async def echo() -> dict:
        logger.info("echo-marker")
        return {"ok": True}

    app.include_router(router)

    await client.get("/_corr/echo", headers={"X-Request-Id": "trace-abc-123"})
    assert _ids(correlated_records, "echo-marker") == ["trace-abc-123"]


@pytest.mark.asyncio
async def test_a_rejected_client_id_does_not_reach_the_log(app, client, correlated_records):
    """``resolve_request_id`` refuses a value it will not write into a log line. The
    substitute has to be what the records carry, or the refusal accomplished nothing."""
    router = APIRouter()

    @router.get("/_corr/hostile")
    async def hostile() -> dict:
        logger.info("hostile-marker")
        return {"ok": True}

    app.include_router(router)

    await client.get("/_corr/hostile", headers={"X-Request-Id": "\x1b[2Jwipe-the-screen"})
    (recorded,) = _ids(correlated_records, "hostile-marker")
    assert "\x1b" not in recorded
    assert recorded == "" or "wipe-the-screen" not in recorded


@pytest.mark.asyncio
async def test_the_unhandled_500_log_line_and_the_response_reference_agree(
    app, client, correlated_records
):
    """The reference in the 500 body is what a clinician reads back to an administrator. It is
    only useful if the traceback is filed under it."""
    configure_logging()
    router = APIRouter()

    @router.get("/_corr/boom")
    async def boom() -> dict:
        raise RuntimeError("internal detail that must not be returned")

    app.include_router(router)

    resp = await client.get("/_corr/boom")
    assert resp.status_code == 500
    reference = resp.json()["request_id"]
    tracebacks = [r for r in correlated_records if r.levelno >= logging.ERROR and r.exc_info]
    assert tracebacks, "the unhandled exception produced no logged traceback"
    assert {r.request_id for r in tracebacks} == {reference}


# --- Isolation between requests --------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_share_an_id(app, client, correlated_records):
    """A ContextVar is per-task; two clinicians' requests overlapping in one process must not
    be filed under each other's reference."""
    router = APIRouter()

    @router.get("/_corr/slow/{marker}")
    async def slow(marker: str) -> dict:
        await asyncio.sleep(0)
        logger.info("slow:first:%s", marker)
        await asyncio.sleep(0)
        logger.info("slow:second:%s", marker)
        return {"ok": True}

    app.include_router(router)

    first, second = await asyncio.gather(
        client.get("/_corr/slow/a", headers={"X-Request-Id": "id-a"}),
        client.get("/_corr/slow/b", headers={"X-Request-Id": "id-b"}),
    )
    assert first.headers["X-Request-Id"] == "id-a"
    assert second.headers["X-Request-Id"] == "id-b"
    assert set(_ids(correlated_records, "slow:first:a")) == {"id-a"}
    assert set(_ids(correlated_records, "slow:second:a")) == {"id-a"}
    assert set(_ids(correlated_records, "slow:first:b")) == {"id-b"}
    assert set(_ids(correlated_records, "slow:second:b")) == {"id-b"}


@pytest.mark.asyncio
async def test_the_id_does_not_outlive_its_request(client):
    """Bound values are reset on the way out, so nothing that runs afterwards inherits a
    reference to a request that has already been answered."""
    assert current_request_id() is None
    await client.get("/health/live")
    assert current_request_id() is None


# --- The four ways a request's work spreads out -------------------------------------------


@pytest.mark.asyncio
async def test_the_id_reaches_a_child_task():
    """The shape of ``ReasoningService.stream``: the pipeline runs in a task created inside the
    request, and every line the eight agents produce comes from there."""
    token = bind_request_id("task-id")
    try:

        async def worker() -> str | None:
            await asyncio.sleep(0)
            return current_request_id()

        assert await asyncio.create_task(worker()) == "task-id"
    finally:
        reset_request_id(token)


@pytest.mark.asyncio
async def test_the_id_reaches_a_default_pool_thread():
    """``asyncio.to_thread`` covers blob I/O, upload hashing, bcrypt and the embedder."""
    token = bind_request_id("thread-id")
    try:
        assert await asyncio.to_thread(current_request_id) == "thread-id"
    finally:
        reset_request_id(token)


@pytest.mark.asyncio
async def test_the_id_reaches_the_llm_pool_through_call_llm(monkeypatch):
    """``run_in_executor`` does *not* carry the context — which is exactly why ``call_llm``
    copies it by hand. This is the assertion that comment was always making and could not keep:
    there was no variable in the context to copy.
    """
    from app.agents import util
    from app.agents.context import ReasoningContext

    seen: dict[str, str | None] = {}

    class _Client:
        def available(self) -> bool:
            return True

        def complete_json(self, system: str, user: str) -> dict:
            # Runs on the LLM thread pool, which is where every provider log line is emitted.
            seen["request_id"] = current_request_id()
            return {"ok": True}

    ctx = ReasoningContext.__new__(ReasoningContext)
    object.__setattr__(ctx, "llm", _Client())
    object.__setattr__(ctx, "verifier_llm", _Client())
    monkeypatch.setattr(ReasoningContext, "out_of_budget", lambda self: False, raising=False)

    token = bind_request_id("llm-pool-id")
    try:
        assert await util.call_llm(ctx, "system", "user") == {"ok": True}
    finally:
        reset_request_id(token)
    assert seen["request_id"] == "llm-pool-id"


@pytest.mark.asyncio
async def test_the_provider_probe_carries_the_id_onto_its_pool(auth_client, monkeypatch):
    """``GET /health/dependencies`` uses the same ``run_in_executor`` shape as ``call_llm``, and
    is the one route whose whole job is to report on a provider that is failing — so its log
    lines are the ones an operator most wants tied to the poll that produced them."""
    from app.routers import health

    seen: dict[str, str | None] = {}

    def _probe() -> dict:
        seen["request_id"] = current_request_id()
        return {}

    monkeypatch.setattr(health, "probe_all_providers", _probe)

    resp = await auth_client.get("/health/dependencies", headers={"X-Request-Id": "probe-corr-id"})
    assert resp.status_code == 200
    assert seen["request_id"] == "probe-corr-id"
