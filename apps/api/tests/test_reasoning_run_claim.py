"""The single-run claim on a reasoning session: what it refuses, and what it must not.

The race itself — two overlapping HTTP runs — is asserted in ``test_concurrent_ingestion``,
which has the file-backed database that makes concurrency real. This module is about the claim's
own mechanics, which are the part that can go wrong quietly:

* refusing a run that should have been allowed is the worse failure. A claim that outlives its
  run — and the ordinary way a run dies is a Reasoning Theatre tab closing, which cancels the
  worker with a ``CancelledError`` the failure handler never sees — would otherwise make the
  case permanently unrunnable, with no way for a clinician to clear it.
* the claim must be released by every path out of a run, not only the happy one.
* the SSE route has to answer with a status code rather than an in-stream error, because an
  ``EventSource`` reconnects through the second and stops on the first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.exceptions import ReasoningRunInProgressError
from app.models.clinical_suggestion import ClinicalSuggestion
from app.models.reasoning_session import ReasoningSession
from app.services import reasoning_service
from app.services.reasoning_service import RUNNING, ReasoningService
from tests.conftest import create_patient


async def _session_id(auth_client, complaint: str = "Fever for three days") -> str:
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": complaint},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session"]["id"]


async def _row(db, session_id: str) -> ReasoningSession:
    result = await db.execute(
        select(ReasoningSession).where(ReasoningSession.id == uuid.UUID(session_id))
    )
    return result.scalar_one()


async def _plant_claim(db, session_id: str, *, age: timedelta) -> None:
    """Leave the session looking like a run took it ``age`` ago and never let go."""
    row = await _row(db, session_id)
    row.status = RUNNING
    row.run_claimed_at = datetime.now(UTC) - age
    await db.commit()


# --- Refusing -----------------------------------------------------------------------------


async def test_a_run_is_refused_while_a_fresh_claim_stands(auth_client, db):
    session_id = await _session_id(auth_client)
    await _plant_claim(db, session_id, age=timedelta(seconds=5))

    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")

    assert resp.status_code == 409
    assert resp.json()["code"] == "reasoning_in_progress"


async def test_the_refusal_says_the_chart_and_the_safety_checks_are_untouched(auth_client, db):
    """A clinician who hits this mid-consultation needs the two things a bare 409 leaves open.

    Same reasoning as ``RateLimitExceededError``: whether a half-written record was left behind,
    and whether the deterministic allergy/interaction checks are still answering. Both answers
    are reassuring and neither is guessable from the status code.
    """
    session_id = await _session_id(auth_client)
    await _plant_claim(db, session_id, age=timedelta(seconds=5))

    message = (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).json()["message"]

    assert "nothing was lost" in message.lower()
    assert "drug-safety" in message.lower()


async def test_a_refused_run_writes_no_suggestions(auth_client, db):
    """The loser must not half-run. Its suggestions would be immutable if it did."""
    session_id = await _session_id(auth_client)
    await _plant_claim(db, session_id, age=timedelta(seconds=5))

    await auth_client.post(f"/api/v1/reasoning/{session_id}/run")

    rows = (
        (
            await db.execute(
                select(ClinicalSuggestion).where(
                    ClinicalSuggestion.session_id == uuid.UUID(session_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_a_refused_run_leaves_the_winners_claim_alone(auth_client, db):
    """The loser must not stamp its own claim on the way out, or it would extend the lease."""
    session_id = await _session_id(auth_client)
    await _plant_claim(db, session_id, age=timedelta(seconds=5))
    before = (await _row(db, session_id)).run_claimed_at

    await auth_client.post(f"/api/v1/reasoning/{session_id}/run")

    db.expire_all()
    assert (await _row(db, session_id)).run_claimed_at == before


# --- Not over-refusing ----------------------------------------------------------------------


async def test_an_abandoned_claim_is_taken_over_once_its_lease_expires(auth_client, db, caplog):
    """The failure mode that matters most: a case nobody can reason about any more.

    A run killed rather than finished leaves ``reasoning`` behind with nothing to clear it, and
    the ordinary way that happens is a browser tab closing — the SSE worker is cancelled with a
    ``CancelledError``, which the ``except Exception`` failure path does not catch. Without the
    lease that session is refused forever.

    Logged as a warning when it happens, because it means a run died and that is worth seeing.
    """
    session_id = await _session_id(auth_client)
    await _plant_claim(
        db, session_id, age=timedelta(minutes=settings.reasoning_run_lease_minutes + 1)
    )

    with caplog.at_level("WARNING"):
        resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")

    assert resp.status_code == 200, resp.text
    assert any("did not finish" in record.message for record in caplog.records)


async def test_a_claim_exactly_at_the_lease_boundary_is_treated_as_abandoned(auth_client, db):
    """Expiring is the forgiving direction, so the boundary belongs on that side."""
    session_id = await _session_id(auth_client)
    await _plant_claim(db, session_id, age=timedelta(minutes=settings.reasoning_run_lease_minutes))

    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")

    assert resp.status_code == 200, resp.text


async def test_a_reasoning_status_with_no_claim_timestamp_is_runnable(auth_client, db):
    """Sessions from before the column existed, and any left ``reasoning`` by a crash.

    There is no timestamp to argue the claim is live, so the only safe reading is abandoned —
    the alternative permanently strands every such session.
    """
    session_id = await _session_id(auth_client)
    row = await _row(db, session_id)
    row.status = RUNNING
    row.run_claimed_at = None
    await db.commit()

    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")

    assert resp.status_code == 200, resp.text


async def test_the_claim_is_released_when_a_run_completes(auth_client, db):
    session_id = await _session_id(auth_client)

    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 200

    db.expire_all()
    row = await _row(db, session_id)
    assert row.status in {"completed", "awaiting_review"}
    assert not ReasoningService(db)._claim_is_live(row)


async def test_the_claim_is_released_when_a_run_fails(auth_client, db, monkeypatch):
    """A failed run must not hold the session for the whole lease.

    The failure path sets ``failed``, and the claim reads the status — so this is really the
    assertion that the two agree. A run that crashed is exactly when a clinician retries.
    """
    session_id = await _session_id(auth_client)

    async def exploding_run_reasoning(state, ctx):
        raise RuntimeError("provider went away")

    monkeypatch.setattr(reasoning_service.graph, "run_reasoning", exploding_run_reasoning)
    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert resp.status_code == 500, resp.text

    db.expire_all()
    row = await _row(db, session_id)
    assert row.status == "failed"
    assert not ReasoningService(db)._claim_is_live(row)

    monkeypatch.undo()
    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 200


# --- The stream ------------------------------------------------------------------------------


async def test_the_stream_refuses_with_a_status_code_not_an_in_stream_error(auth_client, db):
    """An ``EventSource`` stops on a non-2xx and reconnects through a 200 that errors.

    Relaying the conflict inside the stream would make a second tab reconnect into the run in
    progress on a loop — the exact behaviour the reasoning rate limit was added to survive. So
    the check runs before the response starts, where a status code is still possible.
    """
    session_id = await _session_id(auth_client)
    await _plant_claim(db, session_id, age=timedelta(seconds=5))

    resp = await auth_client.get(f"/api/v1/reasoning/{session_id}/stream")

    assert resp.status_code == 409
    assert resp.json()["code"] == "reasoning_in_progress"
    assert "text/event-stream" not in resp.headers.get("content-type", "")


async def test_the_stream_still_opens_when_no_run_holds_the_session(auth_client):
    session_id = await _session_id(auth_client)

    resp = await auth_client.get(f"/api/v1/reasoning/{session_id}/stream")

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]


# --- The claim primitive ----------------------------------------------------------------------


async def test_the_swap_loses_when_the_row_moved_under_it(auth_client, db):
    """The compare-and-swap, not the read, is what makes the claim exclusive.

    ``assert_runnable`` reads a row and can be stale by the time the claim runs. This drives the
    stale case directly: a service holding an instance that says "not running" still loses,
    because the WHERE clause is evaluated against the committed row and no longer matches.
    """
    session_id = await _session_id(auth_client)
    stale = await _row(db, session_id)
    stale_status = stale.status

    # Someone else claims it in between.
    await _plant_claim(db, session_id, age=timedelta(seconds=1))
    stale.status = stale_status
    stale.run_claimed_at = None

    with pytest.raises(ReasoningRunInProgressError):
        await ReasoningService(db)._claim_for_run(stale)


async def test_a_successful_claim_records_when_it_was_taken(auth_client, db):
    """The timestamp is the lease's only input, so it has to be written and it has to move."""
    session_id = await _session_id(auth_client)
    before = datetime.now(UTC)

    service = ReasoningService(db)
    await service._claim_for_run(await _row(db, session_id))

    db.expire_all()
    row = await _row(db, session_id)
    assert row.status == RUNNING
    assert row.run_claimed_at is not None
    claimed = row.run_claimed_at
    claimed = claimed if claimed.tzinfo else claimed.replace(tzinfo=UTC)
    assert before <= claimed <= datetime.now(UTC)


async def test_taking_over_an_abandoned_claim_moves_the_timestamp(auth_client, db):
    """Two takeovers must not both succeed.

    The claim timestamp is written from Python rather than by ``func.now()`` for this reason:
    SQLite's clock has second resolution, so a database-generated value could be unchanged
    between two takeovers inside the same second, and both compare-and-swaps would match.
    """
    session_id = await _session_id(auth_client)
    await _plant_claim(
        db, session_id, age=timedelta(minutes=settings.reasoning_run_lease_minutes + 1)
    )
    abandoned = (await _row(db, session_id)).run_claimed_at

    stale_view = await _row(db, session_id)
    await ReasoningService(db)._claim_for_run(stale_view)

    db.expire_all()
    assert (await _row(db, session_id)).run_claimed_at != abandoned
    # A second taker holding the pre-takeover view is refused rather than joining the run.
    stale_view.run_claimed_at = abandoned
    stale_view.status = RUNNING
    with pytest.raises(ReasoningRunInProgressError):
        await ReasoningService(db)._claim_for_run(stale_view)


async def test_the_claim_clears_a_previous_runs_error_detail(auth_client, db, monkeypatch):
    """A retry after a failure must not carry the old failure's text on the session."""
    session_id = await _session_id(auth_client)

    async def exploding_run_reasoning(state, ctx):
        raise RuntimeError("provider went away")

    monkeypatch.setattr(reasoning_service.graph, "run_reasoning", exploding_run_reasoning)
    await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    db.expire_all()
    assert (await _row(db, session_id)).error_detail

    monkeypatch.undo()
    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 200

    db.expire_all()
    assert (await _row(db, session_id)).error_detail is None
