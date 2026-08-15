"""Closing the Reasoning Theatre must leave nothing running on the request's database session.

``ReasoningService.stream`` runs the whole eight-agent pipeline in a background task and relays
its events to the SSE consumer. When the consumer goes away — a closed tab, a navigation, a
dropped connection, which is the *ordinary* way a run ends rather than a rare one — the generator
is closed and the worker is cancelled.

Cancelled, but for a long time not waited for. ``Task.cancel`` only schedules the
``CancelledError``: the task is left in the "cancelling" state and does not unwind until it next
reaches the event loop, which is after the generator has returned. Returning is what ends the
request, and ``get_db`` closes the session on the way out — so the worker was being handed a
cancellation at some await inside ``run`` at the same moment the ``AsyncSession`` it was using
was being closed underneath it.

The worker spends almost all of its life inside that session: the chart snapshot, the deterministic
safety evaluation, the immutable suggestion inserts, the audit appends, the final commit. An
``AsyncSession`` is not safe for concurrent use, and on asyncpg two operations on one connection
raise ``InterfaceError``; a connection interrupted mid-statement and then returned to the pool is
inherited by whatever request picks it up next. Nothing in this file needs PostgreSQL to make the
point — that the worker is still running when the generator hands control back is observable
directly, and it is the precondition for all of it.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.models.reasoning_session import ReasoningSession
from app.services import reasoning_service
from app.services.reasoning_service import ReasoningService
from tests.conftest import create_patient
from tests.test_reasoning import _complete_intake, _start


async def _session_ready(client, db) -> tuple[uuid.UUID, uuid.UUID]:
    """A reasoning session with intake complete, plus the account that owns it."""
    patient = await create_patient(client)
    state = await _start(client, patient["id"], "fever and cough for three days")
    session_id = uuid.UUID(state["session"]["id"])
    await _complete_intake(client, str(session_id))
    account_id = (
        await db.execute(
            select(ReasoningSession.account_id).where(ReasoningSession.id == session_id)
        )
    ).scalar_one()
    return account_id, session_id


@pytest.mark.asyncio
async def test_closing_the_stream_waits_for_the_worker_to_unwind(auth_client, db, monkeypatch):
    """No task may outlive the generator — the session it holds is about to be torn down."""
    account_id, session_id = await _session_ready(auth_client, db)

    in_the_panel = asyncio.Event()
    real = reasoning_service.graph.run_reasoning

    async def held(state, ctx):
        await ctx.emit("agent_start", {"agent": "hypothesis_panel"})
        in_the_panel.set()
        # Stands in for the minutes of LLM calls and database work a real panel does. Long
        # enough that the worker is unambiguously mid-run when the consumer disappears.
        await asyncio.sleep(30)
        return await real(state, ctx)

    monkeypatch.setattr(reasoning_service.graph, "run_reasoning", held)

    before = set(asyncio.all_tasks())
    stream = ReasoningService(db).stream(account_id, session_id)
    await stream.__anext__()
    await in_the_panel.wait()
    await stream.aclose()

    lingering = [task for task in set(asyncio.all_tasks()) - before if not task.done()]
    assert not lingering, (
        f"{len(lingering)} pipeline worker(s) still running after the stream closed. The "
        f"request-scoped AsyncSession is closed as soon as this returns, so the worker is being "
        f"cancelled and the session torn down under it at the same time: {lingering}"
    )


@pytest.mark.asyncio
async def test_no_statement_is_still_in_flight_when_the_stream_returns(
    auth_client, db, monkeypatch
):
    """The form that matters: the session must be idle by the time the request can close it.

    The test above shows the worker is finished; this shows the consequence, which is the one the
    fix exists for. A cancellation delivered while a statement is on the wire does not end that
    statement synchronously — the driver unwinds it at the next scheduling point. If the generator
    returns first, ``get_db`` closes the session while an operation is still in flight on its
    connection, which on asyncpg is an ``InterfaceError`` and leaves the connection going back to
    the pool mid-statement for the next request to inherit.

    A statement is deliberately held open here so the close lands squarely inside one. Real runs
    do not need the help: the panel is minutes long and nearly all of it is database work.
    """
    account_id, session_id = await _session_ready(auth_client, db)

    in_flight: set[str] = set()
    armed = asyncio.Event()
    holding = asyncio.Event()
    real_execute = type(db).execute

    async def slow_execute(self, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
        # Armed only once the stream is running, so the statement held open is one the panel
        # issues rather than one the arrangement above did.
        if armed.is_set() and not holding.is_set():
            text = str(statement)[:120]
            holding.set()
            in_flight.add(text)
            try:
                await asyncio.sleep(30)  # the statement the close has to land inside
            finally:
                in_flight.discard(text)
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(type(db), "execute", slow_execute)

    stream = ReasoningService(db).stream(account_id, session_id)
    await stream.__anext__()
    armed.set()
    await asyncio.wait_for(holding.wait(), timeout=10)
    await stream.aclose()

    assert not in_flight, (
        "a statement was still on the session's connection when the stream returned; the "
        f"request closes that session next: {sorted(in_flight)}"
    )


@pytest.mark.asyncio
async def test_the_stream_still_delivers_a_complete_run(auth_client, db):
    """Waiting on the worker must not change what a stream that runs to completion emits."""
    account_id, session_id = await _session_ready(auth_client, db)

    events = [event async for event, _data in ReasoningService(db).stream(account_id, session_id)]

    assert "reasoning_start" in events
    assert "verifier" in events, "the mandatory Verifier gate did not emit"
    assert "reasoning_complete" in events
    assert "error" not in events, f"the run failed: {events}"
