"""The chart moving under a reasoning run that is already in flight.

A run holds an exclusive claim on its *session* (``test_reasoning_run_claim``), and nothing
holds the *chart*. Nor should anything: the arrangement this product is built for is one
practice login used from two rooms — see ``test_concurrent_ingestion`` — and a clinician
entering what the patient just told them about their reactions while the panel deliberates is
the ordinary way to work, not a race someone has to contrive. Approving an extracted
prescription mid-run does it just as well.

But the panel reasons over a chart frozen when the run's context was built, and the
current-medication arm of ``drug_safety_check`` was frozen with it. So an allergy documented
during the minutes a run takes was invisible to the deterministic check that Critical Safety
Rule #3 says must hard-block it — and the run finished, reported no hard block at all, and
looked from the outside exactly like a run over a chart that had none.
``ReasoningService._recheck_chart_safety`` redoes that evaluation at the last moment before
anything is written; these are the properties it has to have.

The mid-run write is injected by wrapping ``graph.run_reasoning``, which puts it in precisely
the window that matters: after the context was built and before any row is published. Doing it
over real overlapping HTTP requests would prove the same thing far less reliably, because
whether the write landed inside the window would depend on scheduling.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.agents import graph
from app.models.allergy import Allergy
from app.models.audit_log import AuditLog
from app.models.clinical_suggestion import ClinicalSuggestion
from app.models.medication_event import MedicationEvent
from app.services import reasoning_service
from tests.conftest import create_patient

ASPIRIN_BLOCK = "allergy"


async def _start(client, patient_id: str, complaint: str = "chest discomfort") -> str:
    resp = await client.post(
        f"/api/v1/patients/{patient_id}/reasoning",
        json={"presenting_complaint": complaint},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session"]["id"]


async def _complete_intake(client, session_id: str) -> None:
    for _ in range(4):
        pending = (await client.get(f"/api/v1/reasoning/{session_id}/intake")).json()
        if not pending:
            return
        resp = await client.post(
            f"/api/v1/reasoning/{session_id}/intake/answers",
            json={"answers": [{"question_id": q["id"], "answer_text": "no"} for q in pending]},
        )
        assert resp.status_code == 200, resp.text
        if resp.json()["intake_complete"]:
            return


def _on_aspirin(patient_id: uuid.UUID) -> MedicationEvent:
    return MedicationEvent(
        patient_id=patient_id,
        generic_name="Aspirin",
        dose="75",
        event_type="continue",
        is_current=True,
        clinician_confirmed=True,
    )


def _allergic_to_aspirin(patient_id: uuid.UUID) -> Allergy:
    return Allergy(
        patient_id=patient_id,
        allergen_name="Aspirin",
        allergen_type="drug",
        status="active",
        clinician_confirmed=True,
    )


def _during_the_panel(monkeypatch, work):
    """Run ``work()`` after the run's context is built and before anything is published."""
    real = graph.run_reasoning

    async def wrapped(state, ctx):
        await work()
        return await real(state, ctx)

    monkeypatch.setattr(reasoning_service.graph, "run_reasoning", wrapped)


async def _audit_actions(db, patient_id: uuid.UUID) -> list[str]:
    rows = await db.execute(select(AuditLog.action).where(AuditLog.patient_id == patient_id))
    return [action for (action,) in rows.all()]


@pytest.mark.asyncio
async def test_an_allergy_documented_mid_run_still_hard_blocks(auth_client, db, monkeypatch):
    """The defect this module exists for.

    Identical chart, identical run — the only difference is *when* the allergy was written. With
    the allergy present from the start, ``test_reasoning`` asserts a hard block. Documented
    thirty seconds later, while the panel was thinking, the run came back with none: Rule #3
    decided by which of two things happened first, and nothing on the finished run said so.
    """
    patient = await create_patient(auth_client)
    pid = uuid.UUID(patient["id"])
    db.add(_on_aspirin(pid))
    await db.commit()

    session_id = await _start(auth_client, patient["id"])
    await _complete_intake(auth_client, session_id)

    async def document_the_allergy() -> None:
        db.add(_allergic_to_aspirin(pid))
        await db.commit()

    _during_the_panel(monkeypatch, document_the_allergy)

    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert resp.status_code == 200, resp.text
    result = resp.json()

    blocks = [s for s in result["suggestions"] if s["is_hard_block"]]
    assert blocks, "an allergy documented while the panel ran must still hard-block"
    assert any(ASPIRIN_BLOCK in b["body"].lower() for b in blocks)
    # Conservative wins (Rule #2): a hard block forces the case to flag-for-review however the
    # Verifier tiered it, and the session has to land on the status that tier implies.
    assert result["case_state"]["autonomy_tier"] == "flag_for_review"
    assert result["session"]["status"] == "awaiting_review"


@pytest.mark.asyncio
async def test_the_block_is_persisted_and_overridable_like_any_other(auth_client, db, monkeypatch):
    """A late block is not a second-class one.

    It reaches the clinician through synthesis rebuilding the suggestion list, so it must arrive
    as an ordinary immutable ``ClinicalSuggestion`` carrying the same override requirement — not
    as a note on the response that vanishes when the page is reloaded.
    """
    patient = await create_patient(auth_client)
    pid = uuid.UUID(patient["id"])
    db.add(_on_aspirin(pid))
    await db.commit()
    session_id = await _start(auth_client, patient["id"])
    await _complete_intake(auth_client, session_id)

    async def document_the_allergy() -> None:
        db.add(_allergic_to_aspirin(pid))
        await db.commit()

    _during_the_panel(monkeypatch, document_the_allergy)
    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 200

    rows = await db.execute(
        select(ClinicalSuggestion).where(
            ClinicalSuggestion.session_id == uuid.UUID(session_id),
            ClinicalSuggestion.is_hard_block.is_(True),
        )
    )
    persisted = list(rows.scalars().all())
    assert persisted, "the late hard block must be in the record, not only in the response"

    listed = await auth_client.get(f"/api/v1/reasoning/{session_id}/suggestions")
    assert any(s["is_hard_block"] for s in listed.json())

    # Overriding it still demands documented reasoning (Rule #3).
    block_id = str(persisted[0].id)
    bare = await auth_client.post(
        f"/api/v1/reasoning/{session_id}/suggestions/{block_id}/decision",
        json={"decision": "overridden"},
    )
    assert bare.status_code == 422
    reasoned = await auth_client.post(
        f"/api/v1/reasoning/{session_id}/suggestions/{block_id}/decision",
        json={"decision": "overridden", "reason": "Aspirin reaction was mild GI upset, not IgE."},
    )
    assert reasoned.status_code == 201, reasoned.text


@pytest.mark.asyncio
async def test_the_trail_says_the_chart_moved_under_the_run(auth_client, db, monkeypatch):
    """Why the differential does not mention what the run was blocked on.

    ``hard_block_triggered`` says the run ended with blocks; it cannot say the panel never saw
    one of them. A reviewer reading a run whose reasoning ignores the very allergy it was
    blocked on needs the second fact, and nothing else in the trail carries it.
    """
    patient = await create_patient(auth_client)
    pid = uuid.UUID(patient["id"])
    db.add(_on_aspirin(pid))
    await db.commit()
    session_id = await _start(auth_client, patient["id"])
    await _complete_intake(auth_client, session_id)

    async def document_the_allergy() -> None:
        db.add(_allergic_to_aspirin(pid))
        await db.commit()

    _during_the_panel(monkeypatch, document_the_allergy)
    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 200

    actions = await _audit_actions(db, pid)
    assert "reasoning_chart_changed_under_run" in actions
    assert "hard_block_triggered" in actions


@pytest.mark.asyncio
async def test_a_chart_that_did_not_move_records_nothing_and_loses_nothing(auth_client, db):
    """The re-check is unconditional, so its no-op case is the one that runs in production.

    Two things to get wrong here: claiming the chart moved when it did not, which would put a
    meaningless entry on every run in an append-only trail; and rebuilding the output in a way
    that drops what synthesis produced.
    """
    patient = await create_patient(auth_client)
    session_id = await _start(auth_client, patient["id"], "fever and cough for three days")
    await _complete_intake(auth_client, session_id)

    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert resp.status_code == 200, resp.text
    result = resp.json()

    assert result["suggestions"], "the rebuilt output must still carry the run's suggestions"
    assert result["case_state"]["verifier_status"] in (
        "agree",
        "partial_disagreement",
        "major_disagreement",
    )
    actions = await _audit_actions(db, uuid.UUID(patient["id"]))
    assert "reasoning_chart_changed_under_run" not in actions
    assert "reasoning_session_completed" in actions


@pytest.mark.asyncio
async def test_a_block_the_panel_raised_survives_the_allergy_being_retracted(
    auth_client, db, monkeypatch
):
    """The fresh read decides what to *add*, never what to withdraw.

    Deleting the allergy mid-run makes the re-check's flag list empty, and a re-check that
    simply replaced the panel's findings with its own would drop a hard block the clinician was
    about to be shown — turning a safety net into a way of clearing one. Conservative wins
    (Rule #2): the block stays, and a clinician who believes it wrong overrides it with
    documented reasoning, which is a decision on the record rather than a disappearance.
    """
    patient = await create_patient(auth_client)
    pid = uuid.UUID(patient["id"])
    db.add(_on_aspirin(pid))
    allergy = _allergic_to_aspirin(pid)
    db.add(allergy)
    await db.commit()

    session_id = await _start(auth_client, patient["id"])
    await _complete_intake(auth_client, session_id)

    async def retract_the_allergy() -> None:
        allergy.is_deleted = True
        await db.commit()

    _during_the_panel(monkeypatch, retract_the_allergy)

    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert resp.status_code == 200, resp.text
    result = resp.json()

    assert [s for s in result["suggestions"] if s["is_hard_block"]], (
        "a hard block the panel raised must not be withdrawn by the re-check"
    )
    # ...and it is not reported as newly appeared, because it did not.
    assert "reasoning_chart_changed_under_run" not in await _audit_actions(db, pid)


@pytest.mark.asyncio
async def test_a_re_check_that_cannot_run_fails_the_run_rather_than_publishing(
    auth_client, db, monkeypatch
):
    """Unverified output is not published under a Verifier's name.

    The re-check is the last thing standing between the panel and the record, so it sits inside
    the run's failure handler: if it cannot be completed, nothing is written, the claim is
    released, and the clinician gets an error they can retry — rather than a finished-looking
    run over a chart nothing confirmed was still safe.
    """
    patient = await create_patient(auth_client)
    session_id = await _start(auth_client, patient["id"])
    await _complete_intake(auth_client, session_id)

    # Fails on the *second* call only. The first is the run's context build, which already sat
    # inside the failure handler — letting it through is what makes this an assertion about the
    # re-check's own placement rather than about something that was already covered.
    real_active_flags = reasoning_service.SafetyService.active_flags
    calls = {"n": 0}

    async def flaky_active_flags(self, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("safety tables unreachable")
        return await real_active_flags(self, **kwargs)

    monkeypatch.setattr(reasoning_service.SafetyService, "active_flags", flaky_active_flags)

    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert resp.status_code == 500, resp.text

    db.expire_all()
    rows = await db.execute(
        select(ClinicalSuggestion).where(ClinicalSuggestion.session_id == uuid.UUID(session_id))
    )
    assert not list(rows.scalars().all()), "nothing may be published when the re-check failed"

    # And the session is retryable rather than stuck holding its claim for the whole lease.
    monkeypatch.undo()
    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 200


@pytest.mark.asyncio
async def test_the_theatre_is_told_what_the_published_flags_actually_were(
    auth_client, db, monkeypatch
):
    """The live view must not end on the pre-panel picture.

    The Reasoning Theatre's reducer keeps the last payload per event name, so re-emitting
    ``drug_safety`` after the re-check replaces what the safety node showed rather than
    appearing beside it. Emitted on every run, not only when something appeared: "the re-check
    agreed" is not a thing a clinician can infer from an event that never arrives.
    """
    patient = await create_patient(auth_client)
    pid = uuid.UUID(patient["id"])
    db.add(_on_aspirin(pid))
    await db.commit()
    session_id = await _start(auth_client, patient["id"])
    await _complete_intake(auth_client, session_id)

    async def document_the_allergy() -> None:
        db.add(_allergic_to_aspirin(pid))
        await db.commit()

    _during_the_panel(monkeypatch, document_the_allergy)

    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    row = await db.execute(
        select(reasoning_service.ReasoningSession).where(
            reasoning_service.ReasoningSession.id == uuid.UUID(session_id)
        )
    )
    session = row.scalar_one()
    service = reasoning_service.ReasoningService(db)
    await service.run(session.account_id, session.id, emit=emit)

    safety = [data for name, data in events if name == "drug_safety"]
    assert safety, "the safety lane must be reported to the theatre"
    assert safety[-1]["appeared_during_run"] == 1
    assert safety[-1]["hard_blocks"] >= 1
    assert any(f["is_hard_block"] for f in safety[-1]["flags"])
