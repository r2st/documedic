"""The Reasoning Theatre must say what its drug-safety check could not read, not just what it found.

``SafetyService`` splits the deterministic output in two: ``active_flags`` is what the rules
found across the current medication list, and ``chart_completeness_flags`` is what the rules
could not be applied to at all — a medication line the vocabulary cannot resolve, an allergen it
cannot identify, a problem-list row that tokenises to nothing, a liver the panel says is
impaired. ``GET /patients/{id}/drug-safety/flags`` appends the second set to the first, and
``POST ../check`` does too.

The reasoning engine's chart arm called only the first. So on a chart carrying an unidentifiable
allergen, the run's ``drug_safety_check`` node reported the flags of a chart it could fully read:
the allergy cross-check had run against nothing, and an empty list said "nothing found" and
"nothing checked" in the same words. That is the failure the unevaluated-* checks were written to
end, arriving on the one surface in the product whose entire purpose is showing the clinician
what the machine actually did — the anti-automation-bias display, where a confident-looking gap
is worth less than no display at all.

Appended once for the chart, matching the Safety screen. The per-management-option arm is about
the drugs named in a sentence and takes none of them.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.context import resolve_safety
from app.models.allergy import Allergy
from app.models.medication_event import MedicationEvent
from app.services.reasoning_service import ReasoningService
from app.services.safety_service import SafetyService
from tests.conftest import create_patient

# Neither is in the seeded fifty-drug vocabulary, which is the ordinary case this product exists
# to handle rather than a contrived one: an Indian brand nobody has curated yet.
UNSEEDED_BRAND = "Zerodol-SP"


def _unresolvable_medication(patient_id: uuid.UUID) -> MedicationEvent:
    return MedicationEvent(
        patient_id=patient_id,
        brand_name_raw=UNSEEDED_BRAND,
        event_type="continue",
        is_current=True,
        clinician_confirmed=True,
    )


def _unidentifiable_allergy(patient_id: uuid.UUID) -> Allergy:
    return Allergy(
        patient_id=patient_id,
        allergen_name=UNSEEDED_BRAND,
        allergen_type="drug",
        status="active",
        clinician_confirmed=True,
    )


async def _account_id(db, patient_id: uuid.UUID) -> uuid.UUID:
    from app.models.patient import Patient

    patient = await db.get(Patient, patient_id)
    return patient.account_id


@pytest.mark.asyncio
async def test_the_chart_arm_reports_a_medication_it_could_not_evaluate(auth_client, db) -> None:
    """The bug. A chart openly listing a drug the engine cannot read returned no flag at all."""
    patient = await create_patient(auth_client)
    pid = uuid.UUID(patient["id"])
    db.add(_unresolvable_medication(pid))
    await db.commit()

    service = ReasoningService(db)
    evaluate = await service._safety_evaluator(await _account_id(db, pid), pid)
    flags = await resolve_safety(evaluate(""))

    assert [f["check_type"] for f in flags] == ["unevaluated_medication"]
    assert UNSEEDED_BRAND in flags[0]["summary"]


@pytest.mark.asyncio
async def test_the_chart_arm_reports_an_allergen_it_could_not_identify(auth_client, db) -> None:
    """The one that matters most: Rule #3's hard block ran against an allergen it cannot match."""
    patient = await create_patient(auth_client)
    pid = uuid.UUID(patient["id"])
    db.add(_unidentifiable_allergy(pid))
    await db.commit()

    service = ReasoningService(db)
    evaluate = await service._safety_evaluator(await _account_id(db, pid), pid)
    flags = await resolve_safety(evaluate(""))

    assert "unevaluated_allergy" in [f["check_type"] for f in flags]


@pytest.mark.asyncio
async def test_the_chart_arm_matches_what_the_safety_screen_shows(auth_client, db) -> None:
    """Two surfaces over one deterministic engine must not disagree about the same chart.

    The Theatre and the Safety screen are the two places a clinician reads drug safety, and a
    flag visible on one and absent from the other is worse than a flag on neither — it makes
    which screen you happened to open decide what you were told.
    """
    patient = await create_patient(auth_client)
    pid = uuid.UUID(patient["id"])
    db.add(_unresolvable_medication(pid))
    db.add(_unidentifiable_allergy(pid))
    await db.commit()

    screen = await auth_client.get(f"/api/v1/patients/{patient['id']}/drug-safety/flags")
    assert screen.status_code == 200, screen.text
    on_screen = sorted(f["check_type"] for f in screen.json()["flags"])

    evaluate = await ReasoningService(db)._safety_evaluator(await _account_id(db, pid), pid)
    in_theatre = sorted(f["check_type"] for f in await resolve_safety(evaluate("")))

    assert in_theatre == on_screen


@pytest.mark.asyncio
async def test_a_readable_chart_gains_no_extra_flags(auth_client, db) -> None:
    """The completeness flags must stay silent when there is nothing incomplete.

    Otherwise every run carries a note, which is how a note stops being read.
    """
    patient = await create_patient(auth_client)
    pid = uuid.UUID(patient["id"])
    db.add(
        MedicationEvent(
            patient_id=pid,
            generic_name="Paracetamol",
            event_type="continue",
            is_current=True,
            clinician_confirmed=True,
        )
    )
    await db.commit()

    evaluate = await ReasoningService(db)._safety_evaluator(await _account_id(db, pid), pid)
    raised = {f["check_type"] for f in await resolve_safety(evaluate(""))}

    # Not an assertion of emptiness: paracetamol on a chart with no liver panel legitimately
    # raises `hepatic_dose` from the per-drug arm, which is the rules working. What must be
    # absent is any claim that the chart itself could not be read.
    assert raised.isdisjoint(
        {"unevaluated_medication", "unevaluated_allergy", "unevaluated_condition"}
    )


@pytest.mark.asyncio
async def test_the_per_option_arm_does_not_repeat_them(auth_client, db) -> None:
    """A management option is screened for the drugs it names, not re-audited for the chart.

    Repeating the chart-level notes under every option is what the split in
    ``chart_completeness_flags`` exists to prevent, and a run drafts several options.
    """
    patient = await create_patient(auth_client)
    pid = uuid.UUID(patient["id"])
    db.add(_unresolvable_medication(pid))
    await db.commit()

    evaluate = await ReasoningService(db)._safety_evaluator(await _account_id(db, pid), pid)
    per_option = await resolve_safety(
        evaluate("Guidelines support considering paracetamol for fever.")
    )

    assert "unevaluated_medication" not in [f["check_type"] for f in per_option]


@pytest.mark.asyncio
async def test_the_recheck_before_publication_carries_them_too(auth_client, db, monkeypatch):
    """The shipped evaluation is the re-check, so a gap absent from it never reaches the run.

    Same window as ``test_chart_changed_under_run``: a prescription approved mid-run can be one
    the vocabulary cannot resolve, and the run must not publish a drug-safety picture that
    silently omits it.
    """
    from app.agents import graph
    from app.services import reasoning_service

    patient = await create_patient(auth_client)
    pid = uuid.UUID(patient["id"])

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "fever for three days"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["session"]["id"]
    for _ in range(4):
        pending = (await auth_client.get(f"/api/v1/reasoning/{session_id}/intake")).json()
        if not pending:
            break
        answered = await auth_client.post(
            f"/api/v1/reasoning/{session_id}/intake/answers",
            json={"answers": [{"question_id": q["id"], "answer_text": "no"} for q in pending]},
        )
        assert answered.status_code == 200, answered.text
        if answered.json()["intake_complete"]:
            break

    real = graph.run_reasoning

    async def wrapped(state, ctx):
        db.add(_unresolvable_medication(pid))
        await db.commit()
        return await real(state, ctx)

    monkeypatch.setattr(reasoning_service.graph, "run_reasoning", wrapped)

    run = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert run.status_code == 200, run.text

    flags = run.json()["case_state"]["drug_safety_flags"]
    assert "unevaluated_medication" in [f["check_type"] for f in flags]


@pytest.mark.asyncio
async def test_the_split_is_still_a_split(auth_client, db) -> None:
    """Guard the seam itself: the two halves must stay separately callable.

    Folding the chart-level checks into ``evaluate_drug_safety`` would repeat every one of them
    once per current medication, which is the arrangement this pair of methods replaced.
    """
    patient = await create_patient(auth_client)
    pid = uuid.UUID(patient["id"])
    db.add(_unresolvable_medication(pid))
    db.add(
        MedicationEvent(
            patient_id=pid,
            generic_name="Paracetamol",
            event_type="continue",
            is_current=True,
            clinician_confirmed=True,
        )
    )
    await db.commit()

    service = SafetyService(db)
    per_drug = await service.active_flags(account_id=await _account_id(db, pid), patient_id=pid)
    chart = await service.chart_completeness_flags(pid)

    assert [f.check_type for f in chart].count("unevaluated_medication") == 1
    assert not any(
        f.check_type == "unevaluated_medication" for _vocab, flags in per_drug for f in flags
    )
