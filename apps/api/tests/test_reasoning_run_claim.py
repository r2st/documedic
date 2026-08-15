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
* the claim is also where a run is written into the medical record, because it is the only
  commit a run makes before the panel starts.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.agents.state import CaseState
from app.config import settings
from app.exceptions import ReasoningRunInProgressError, ReasoningRunSupersededError
from app.models.audit_log import AuditLog
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


def _utc(value: datetime | None) -> datetime | None:
    """A stored timestamp as an aware UTC instant.

    SQLite drops tzinfo on the round trip and PostgreSQL does not, so a comparison against a
    value read back from the row has to normalise before it means anything.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _audit(db, session_id: str) -> list[AuditLog]:
    """Every audit entry filed against this session, oldest first."""
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.entity_id == uuid.UUID(session_id))
        .order_by(AuditLog.sequence)
    )
    return list(result.scalars().all())


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


# --- What the medical record ends up saying ---------------------------------------------------


async def test_running_the_panel_is_written_into_the_trail_with_who_and_when(auth_client, db):
    """Opening a session and running the panel are different events and need different records.

    ``reasoning_session_started`` is written when the session is *opened*. The panel can be run
    from it later, more than once. Without its own record the trail could not say an analysis
    was ever performed, only that a case had been created.
    """
    session_id = await _session_id(auth_client)
    before = datetime.now(UTC)

    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 200

    entries = [e for e in await _audit(db, session_id) if e.action == "reasoning_run_started"]
    assert len(entries) == 1, "the run left no record of itself in the trail"
    entry = entries[0]
    assert entry.account_id is not None, "the trail cannot say who ran the panel"
    assert entry.patient_id is not None, "the run is invisible from the patient's own trail"
    created = entry.created_at
    assert before <= (created if created.tzinfo else created.replace(tzinfo=UTC))


async def test_each_run_of_one_session_gets_its_own_record(auth_client, db):
    """Re-running after the chart changed is a second clinical act, not a repeat of the first."""
    session_id = await _session_id(auth_client)

    for _ in range(2):
        assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 200

    entries = [e for e in await _audit(db, session_id) if e.action == "reasoning_run_started"]
    assert len(entries) == 2, f"two runs left {len(entries)} records"


async def test_a_refused_run_is_not_recorded_as_one(auth_client, db):
    """The loser never ran the panel, and a trail that says it did overstates the disclosure."""
    session_id = await _session_id(auth_client)
    await _plant_claim(db, session_id, age=timedelta(seconds=5))

    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 409

    assert [e for e in await _audit(db, session_id) if e.action == "reasoning_run_started"] == []


async def test_an_abandoned_run_still_leaves_a_record_it_happened(auth_client, db, monkeypatch):
    """The case this record exists for, and the one nothing else in ``run`` can cover.

    A Reasoning Theatre tab closing cancels the worker with a ``CancelledError``. It is not an
    error path — ``run``'s ``except Exception`` never sees it — so no ``reasoning_session_failed``
    is written, no ``reasoning_session_completed`` is written, and everything the run had
    flushed is rolled back with its transaction. Before the claim carried this entry the trail
    showed a session being opened and then nothing at all, while the clinician had in fact
    watched the panel's hypotheses and hard blocks stream back over SSE. An unrecorded
    disclosure of a patient's clinical picture is the exact question the DPDP trail is asked
    months later.

    The claim's commit is what survives, so the record survives with it. An abandoned run is
    legible as this entry with neither closing record following it.
    """
    session_id = await _session_id(auth_client)
    in_the_graph = asyncio.Event()

    async def never_returns(state, ctx):
        in_the_graph.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(reasoning_service.graph, "run_reasoning", never_returns)

    service = ReasoningService(db)
    row = await _row(db, session_id)
    stream = service.stream(row.account_id, uuid.UUID(session_id))
    watcher = asyncio.create_task(stream.__anext__())
    await asyncio.wait_for(in_the_graph.wait(), timeout=10)
    # The clinician closes the tab.
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher
    await stream.aclose()

    actions = [e.action for e in await _audit(db, session_id)]
    assert "reasoning_run_started" in actions, (
        f"a run streamed clinical output to a clinician and left no trace: {actions}"
    )
    assert "reasoning_session_completed" not in actions
    assert "reasoning_session_failed" not in actions


async def test_taking_over_a_dead_run_says_so_in_the_trail(auth_client, db):
    """A reviewer reading the trail needs the takeover to be visible, not inferred.

    Two runs of one case, the first with no closing record, is otherwise indistinguishable from
    a clinician who simply ran it twice.
    """
    session_id = await _session_id(auth_client)
    await _plant_claim(
        db, session_id, age=timedelta(minutes=settings.reasoning_run_lease_minutes + 1)
    )

    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 200

    entry = [e for e in await _audit(db, session_id) if e.action == "reasoning_run_started"][-1]
    assert entry.payload["took_over_abandoned_run"] is True
    assert entry.payload["previous_status"] == RUNNING


async def test_an_ordinary_first_run_is_not_flagged_as_a_takeover(auth_client, db):
    session_id = await _session_id(auth_client)

    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 200

    entry = [e for e in await _audit(db, session_id) if e.action == "reasoning_run_started"][-1]
    assert entry.payload["took_over_abandoned_run"] is False
    assert entry.payload["previous_status"] != RUNNING


# --- What a client is told --------------------------------------------------------------------


async def test_the_session_read_says_a_run_is_in_progress_while_one_holds_it(auth_client, db):
    session_id = await _session_id(auth_client)
    await _plant_claim(db, session_id, age=timedelta(seconds=5))

    body = (await auth_client.get(f"/api/v1/reasoning/{session_id}")).json()

    assert body["status"] == RUNNING
    assert body["run_in_progress"] is True


async def test_an_abandoned_run_is_reported_as_not_in_progress(auth_client, db):
    """The half a bare ``status`` cannot tell a client, and the one that locks clinicians out.

    A Run control gated on ``status == 'reasoning'`` — the obvious way to build it — would stay
    disabled for the whole lease on a case whose run died when a tab closed. That is the failure
    the lease exists to prevent, moved from the server into the browser. The status still
    reports what the record says; this reports whether it is still true.
    """
    session_id = await _session_id(auth_client)
    await _plant_claim(
        db, session_id, age=timedelta(minutes=settings.reasoning_run_lease_minutes + 1)
    )

    body = (await auth_client.get(f"/api/v1/reasoning/{session_id}")).json()

    assert body["status"] == RUNNING, "the recorded status should be left as it was"
    assert body["run_in_progress"] is False


async def test_what_the_read_reports_agrees_with_what_a_run_would_do(auth_client, db):
    """One rule, not two. A client told a run is in progress by one rule while the claim
    permits or refuses by another is worse than either rule being wrong on its own."""
    session_id = await _session_id(auth_client)

    for age in (
        timedelta(seconds=5),
        timedelta(minutes=settings.reasoning_run_lease_minutes + 1),
    ):
        await _plant_claim(db, session_id, age=age)
        reported = (await auth_client.get(f"/api/v1/reasoning/{session_id}")).json()[
            "run_in_progress"
        ]
        refused = (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 409
        assert reported is refused, f"read and claim disagree at claim age {age}"


async def test_a_session_that_never_ran_is_not_in_progress(auth_client):
    session_id = await _session_id(auth_client)

    body = (await auth_client.get(f"/api/v1/reasoning/{session_id}")).json()

    assert body["run_in_progress"] is False


async def test_a_finished_run_is_not_reported_as_in_progress(auth_client):
    session_id = await _session_id(auth_client)

    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 200

    body = (await auth_client.get(f"/api/v1/reasoning/{session_id}")).json()
    assert body["run_in_progress"] is False


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
        await ReasoningService(db)._claim_for_run(stale.account_id, stale)


async def test_a_successful_claim_records_when_it_was_taken(auth_client, db):
    """The timestamp is the lease's only input, so it has to be written and it has to move."""
    session_id = await _session_id(auth_client)
    before = datetime.now(UTC)

    service = ReasoningService(db)
    fresh = await _row(db, session_id)
    await service._claim_for_run(fresh.account_id, fresh)

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
    await ReasoningService(db)._claim_for_run(stale_view.account_id, stale_view)

    db.expire_all()
    assert (await _row(db, session_id)).run_claimed_at != abandoned
    # A second taker holding the pre-takeover view is refused rather than joining the run.
    stale_view.run_claimed_at = abandoned
    stale_view.status = RUNNING
    with pytest.raises(ReasoningRunInProgressError):
        await ReasoningService(db)._claim_for_run(stale_view.account_id, stale_view)


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


# --- Releasing the claim on the way out ---------------------------------------------------------


async def test_a_run_whose_claim_moved_under_it_refuses_to_publish(auth_client, db):
    """The success path's compare-and-swap, driven directly.

    ``_release_claim_as_failed`` already matches on ``run_claimed_at``, because a run that
    overran ``reasoning_run_lease_minutes`` can have been taken over while it was still going and
    must not stamp ``failed`` on the successor. ``_finish_claimed_run`` is that predicate on the
    path that writes the *immutable* rows.

    Driven at the service rather than over HTTP on purpose. Two overlapping requests cannot reach
    this today: ``run`` flushes a dirty session instance before the panel, so it holds the row's
    write lock for the whole run and a takeover blocks (PostgreSQL) or errors (SQLite) rather
    than succeeding. That lock is a side effect of a staleness workaround, not a decision — see
    ``_finish_claimed_run`` — so the invariant is asserted where it is stated.
    """
    session_id = await _session_id(auth_client)
    service = ReasoningService(db)
    row = await _row(db, session_id)
    await service._claim_for_run(row.account_id, row)
    mine = row.run_claimed_at

    # The lease expires and someone else takes the session while this run is still going.
    await _plant_claim(db, session_id, age=timedelta(seconds=1))
    successor = (await _row(db, session_id)).run_claimed_at
    assert successor != mine

    with pytest.raises(ReasoningRunSupersededError):
        await service._finish_claimed_run(
            row,
            mine,
            "completed",
            CaseState(patient_id=uuid.uuid4(), presenting_complaint="x"),
            {"case_state": {}},
        )

    db.expire_all()
    after = await _row(db, session_id)
    assert after.status == RUNNING, "the superseded run wrote over the successor's status"
    # Naive/aware: SQLite drops tzinfo on the round trip, so compare the instants.
    assert _utc(after.run_claimed_at) == _utc(successor), "the superseded run took the claim back"


async def test_a_run_that_still_holds_its_claim_writes_its_terminal_header(auth_client, db):
    """The guard must not refuse the ordinary case, which is every run that finishes on time."""
    session_id = await _session_id(auth_client)
    service = ReasoningService(db)
    row = await _row(db, session_id)
    await service._claim_for_run(row.account_id, row)

    state = CaseState(patient_id=uuid.uuid4(), presenting_complaint="x")
    state.autonomy_tier = "flag_for_review"
    await service._finish_claimed_run(
        row, row.run_claimed_at, "awaiting_review", state, {"case_state": {"agent_trace": []}}
    )
    await db.commit()

    db.expire_all()
    after = await _row(db, session_id)
    assert after.status == "awaiting_review"
    assert after.autonomy_tier == "flag_for_review"
    assert after.completed_at is not None
    assert not service._claim_is_live(after)


async def test_a_run_holding_no_claim_at_all_refuses_rather_than_matching_every_row(
    auth_client, db
):
    """``run_claimed_at == None`` renders as ``IS NULL``, which matches an *unclaimed* session.

    Unreachable through ``run`` — the claim is always stamped first — and guarded anyway, because
    the failure mode is the one this method exists to prevent: a run with no claim publishing
    over a session that has one.
    """
    session_id = await _session_id(auth_client)
    row = await _row(db, session_id)

    with pytest.raises(ReasoningRunSupersededError):
        await ReasoningService(db)._finish_claimed_run(
            row,
            None,
            "completed",
            CaseState(patient_id=uuid.uuid4(), presenting_complaint="x"),
            {"case_state": {}},
        )


async def test_the_superseded_run_is_reported_as_a_conflict_not_a_server_error(auth_client, db):
    """The clinician's answer has to be actionable: the run they are watching is fine."""
    assert ReasoningRunSupersededError().status_code == 409
    assert ReasoningRunSupersededError().code == "reasoning_run_superseded"
    assert "Nothing from it has been saved" in ReasoningRunSupersededError().message
