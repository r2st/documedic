"""Where the run claim is released, and where it used to leak.

``test_reasoning_run_claim`` covers the claim being taken and refused. This module covers the
other half: every path out of a claimed run has to let go of it, and for a long time only one
did. ``run`` took and *committed* the claim, then assembled the chart snapshot and built the
agent context, and only then entered the ``try`` whose handler marks the session ``failed``.

Those two steps outside the handler are the two heaviest things the method does —
``_rebuild_intake_state`` reads the whole longitudinal record, ``_build_context`` re-evaluates
every current medication for safety and loads the guideline corpus — so they are also the two
likeliest to fail. When they did, the session kept ``reasoning`` and a live claim for the full
``reasoning_run_lease_minutes``: unrunnable by anyone, with no ``failed`` status the UI could
show and no closing entry in the audit trail. That last part is the sharp edge. R54 established
``reasoning_run_started`` with nothing after it as the trail's signature of a run that reached
the panel and was abandoned mid-flight; a run that never got as far as the panel was writing
the identical signature.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.reasoning_session import RUNNING, ReasoningSession
from app.services import reasoning_service
from app.services.reasoning_service import ReasoningService
from tests.conftest import create_patient


async def _session_id(auth_client) -> str:
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "Breathless on climbing stairs"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session"]["id"]


async def _row(db, session_id: str) -> ReasoningSession:
    db.expire_all()
    return (
        await db.execute(
            select(ReasoningSession).where(ReasoningSession.id == uuid.UUID(session_id))
        )
    ).scalar_one()


async def _actions(db, session_id: str) -> list[str]:
    result = await db.execute(
        select(AuditLog.action)
        .where(AuditLog.entity_id == uuid.UUID(session_id))
        .order_by(AuditLog.sequence)
    )
    return list(result.scalars().all())


# The two steps that used to sit between the committed claim and the failure handler. Each is
# patched to raise where it is *looked up* — on the service class — so the test exercises the
# real ``run`` control flow rather than a re-implementation of it.
_PRE_PANEL_STEPS = ["_rebuild_intake_state", "_build_context"]


@pytest.mark.parametrize("step", _PRE_PANEL_STEPS)
async def test_a_failure_before_the_panel_releases_the_claim(auth_client, db, monkeypatch, step):
    session_id = await _session_id(auth_client)

    async def boom(*args, **kwargs):
        raise RuntimeError(f"{step} could not reach the database")

    monkeypatch.setattr(ReasoningService, step, boom)
    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert resp.status_code == 500, resp.text

    row = await _row(db, session_id)
    assert row.status == "failed", f"a failure in {step} left the session at {row.status!r}"
    assert row.run_in_progress is False


@pytest.mark.parametrize("step", _PRE_PANEL_STEPS)
async def test_the_case_can_be_run_again_immediately(auth_client, db, monkeypatch, step):
    """The consequence a clinician actually meets: retrying the case they just watched fail.

    Without the release this is a 409 for fifteen minutes, on a case whose run never started.
    """
    session_id = await _session_id(auth_client)

    async def boom(*args, **kwargs):
        raise RuntimeError("transient")

    monkeypatch.setattr(ReasoningService, step, boom)
    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 500

    monkeypatch.undo()
    retried = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert retried.status_code == 200, retried.text


@pytest.mark.parametrize("step", _PRE_PANEL_STEPS)
async def test_the_trail_closes_the_run_it_opened(auth_client, db, monkeypatch, step):
    """``reasoning_run_started`` must not be the last word about a run that never ran.

    An unclosed start is what an *abandoned* run looks like — the clinician watched output
    stream and closed the tab. A run that failed before the panel has to be legible as a
    different thing, or the one signal that distinguishes them stops meaning anything.
    """
    session_id = await _session_id(auth_client)

    async def boom(*args, **kwargs):
        raise RuntimeError("transient")

    monkeypatch.setattr(ReasoningService, step, boom)
    await auth_client.post(f"/api/v1/reasoning/{session_id}/run")

    actions = await _actions(db, session_id)
    assert "reasoning_run_started" in actions
    assert "reasoning_session_failed" in actions
    assert actions.index("reasoning_session_failed") > actions.index("reasoning_run_started")


async def test_the_failure_reason_reaches_the_session_without_the_exception_text(
    auth_client, db, monkeypatch
):
    """``describe_exception``, not ``str(exc)``.

    The pre-panel steps handle the patient snapshot, so an exception raised there can quote it —
    and ``error_detail`` is read back by the UI while ``audit_logs.payload`` is unencrypted,
    immutable and never pruned.
    """
    session_id = await _session_id(auth_client)
    leaked = "Ramesh Kumar, 58M, metformin 1g BD"

    async def boom(*args, **kwargs):
        raise RuntimeError(f"upstream rejected prompt: {leaked}")

    monkeypatch.setattr(ReasoningService, "_build_context", boom)
    await auth_client.post(f"/api/v1/reasoning/{session_id}/run")

    row = await _row(db, session_id)
    assert row.error_detail is not None
    assert leaked not in row.error_detail
    assert "RuntimeError" in row.error_detail

    payloads = (
        (
            await db.execute(
                select(AuditLog.payload).where(AuditLog.entity_id == uuid.UUID(session_id))
            )
        )
        .scalars()
        .all()
    )
    assert leaked not in str(payloads)


async def test_a_run_that_outlived_its_lease_does_not_fail_its_successors_session(
    auth_client, db, monkeypatch
):
    """The predicate on ``run_claimed_at``, and why it is not decorative.

    A run whose LLM calls retry past ``reasoning_run_lease_minutes`` can have had its session
    taken over while it was still going. Its eventual failure must land on nothing: writing
    ``failed`` unconditionally would stamp it on the *successor's* session, underneath a
    clinician watching that run stream.
    """
    session_id = await _session_id(auth_client)
    service = ReasoningService(db)
    session = await service.get_session(
        (await _row(db, session_id)).account_id, uuid.UUID(session_id)
    )
    stale_claim = datetime.now(UTC) - timedelta(hours=1)

    # The successor: a live claim, taken after the (imaginary) first run had already started.
    session.status = RUNNING
    session.run_claimed_at = datetime.now(UTC)
    await db.commit()

    # The predecessor now fails, still believing it holds the claim it took an hour ago.
    await service._release_claim_as_failed(  # noqa: SLF001 — the unit under test
        session.account_id,
        session,
        session.patient_id,
        stale_claim,
        RuntimeError("provider timed out after four retries"),
    )

    row = await _row(db, session_id)
    assert row.status == RUNNING, "a dead run stamped 'failed' on the run that succeeded it"
    assert row.run_in_progress is True
    # And it stayed quiet in the trail: it has nothing to say about a session it no longer holds.
    assert "reasoning_session_failed" not in await _actions(db, session_id)


async def test_a_release_that_cannot_be_written_does_not_replace_the_error_it_reports(
    auth_client, db, monkeypatch
):
    """Best-effort by construction, and never louder than the failure it is reporting.

    The release is itself database work, and the failure it handles may *be* the database. When
    it cannot be written the claim falls back to the lease — but the exception the clinician is
    waiting on an answer about has to be the one that propagates, not a secondary error from the
    cleanup.
    """
    session_id = await _session_id(auth_client)

    async def boom(*args, **kwargs):
        raise RuntimeError("the panel could not be reached")

    async def unreleasable(*args, **kwargs):
        raise RuntimeError("the connection is gone too")

    monkeypatch.setattr(reasoning_service.graph, "run_reasoning", boom)
    monkeypatch.setattr(ReasoningService, "_release_claim_as_failed", unreleasable)

    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    # A 500 from the original failure, not a crash inside the handler — and the generic body the
    # error contract requires either way, so the assertion that matters is that the request
    # completed at all rather than tearing down mid-handler.
    assert resp.status_code == 500, resp.text
