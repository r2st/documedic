"""A product the chart carries as current twice, on the screen that re-checks the chart.

``check_duplicate_therapy`` has always had a ``same_product`` branch for "the patient already
has an active order for this exact drug", and it was reachable only from ``POST ../check``,
where a clinician proposes something. The standing board — ``GET ../flags``, whose whole job is
to re-check the medications already on the chart — could not raise it: the board evaluates each
drug against "everyone but me" and builds that set by dropping *every* row sharing the drug's
reference id, so the second order was removed from the context before the check that exists to
notice it ever ran. Two live orders for the same drug, and a board that said nothing.

The rows are reachable through the ordinary write path, which is the point. The merge keys a
medication on ``(generic name, dose)`` so that a dose change lands as a new row instead of
being swallowed as a duplicate of the old one — so a second prescription at a different dose
inserts a second current row for the same drug. "Glycomet 500" beside "Glycomet 850" is the everyday
shape of it.

Fixed by ``check_duplicate_orders``, a chart-level check evaluated once per context alongside
the other statements-about-the-chart, rather than by letting the per-drug context keep a drug's
own second copy — which would have fixed this and started double-counting that drug toward the
cumulative hepatotoxic and bleeding burdens. The last test here pins that.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.safety import DrugRef, SafetyContext, check_duplicate_orders
from app.models.medication_event import MedicationEvent
from app.services.safety_service import SafetyService
from tests.conftest import create_patient
from tests.test_clinical_workflow_multistep import _document, _ingest
from tests.test_safety_active_flags import _account_and_patient, _add_current_med


def _dups(flags) -> list:
    return [f for f in flags if f.check_type == "duplicate_therapy"]


# --- The chart-level check itself ---------------------------------------------------------


def test_the_same_product_charted_twice_is_reported_once():
    ctx = SafetyContext(
        current_meds=[
            DrugRef(reference_id="PCM-500", generic_name="Paracetamol"),
            DrugRef(reference_id="PCM-500", generic_name="Paracetamol"),
        ]
    )
    flags = check_duplicate_orders(ctx)
    assert len(flags) == 1, "one flag about the drug, not one per copy of it"
    assert flags[0].details["order_count"] == 2
    assert flags[0].details["existing_drug"] == "Paracetamol"
    assert flags[0].details["match_type"] == "same_product"
    assert flags[0].details["scope"] == "chart"


def test_a_single_order_raises_nothing():
    ctx = SafetyContext(current_meds=[DrugRef(reference_id="PCM-500", generic_name="Paracetamol")])
    assert check_duplicate_orders(ctx) == []


def test_a_third_order_is_counted_not_re_reported():
    ctx = SafetyContext(
        current_meds=[DrugRef(reference_id="PCM-500", generic_name="Paracetamol")] * 3
    )
    flags = check_duplicate_orders(ctx)
    assert len(flags) == 1
    assert flags[0].details["order_count"] == 3
    assert "3 separate current orders" in flags[0].summary


def test_two_different_drugs_are_not_a_duplicate():
    ctx = SafetyContext(
        current_meds=[
            DrugRef(reference_id="PCM-500", generic_name="Paracetamol"),
            DrugRef(reference_id="MET-500", generic_name="Metformin"),
        ]
    )
    assert check_duplicate_orders(ctx) == []


def test_it_is_a_warning_and_never_a_hard_block():
    """Two live orders is most often an uncleared refill or titration. Blocking on it would be
    an alert the clinician cannot clear, and Rule #3 reserves hard blocks for allergy and
    contraindication conflicts."""
    ctx = SafetyContext(
        current_meds=[DrugRef(reference_id="PCM-500", generic_name="Paracetamol")] * 2
    )
    flag = check_duplicate_orders(ctx)[0]
    assert flag.is_hard_block is False
    assert flag.severity == "warning"


def test_unidentified_molecules_are_not_grouped_together():
    """An ingredient with no standalone vocabulary row carries an empty reference id. Two
    *different* such molecules must not compare equal and be reported as one drug ordered
    twice — the same trap ``check_duplicate_therapy`` guards its ingredient comparison against.
    """
    ctx = SafetyContext(
        current_meds=[
            DrugRef(reference_id="", generic_name="Clavulanic acid"),
            DrugRef(reference_id="", generic_name="Trimethoprim"),
        ]
    )
    assert check_duplicate_orders(ctx) == []


def test_the_flag_does_not_depend_on_medication_row_order():
    a = DrugRef(reference_id="AAA-1", generic_name="Amlodipine")
    b = DrugRef(reference_id="ZZZ-9", generic_name="Zolpidem")
    forward = check_duplicate_orders(SafetyContext(current_meds=[a, a, b, b]))
    reverse = check_duplicate_orders(SafetyContext(current_meds=[b, b, a, a]))
    assert [f.summary for f in forward] == [f.summary for f in reverse]


# --- Through the service and the endpoints ------------------------------------------------


@pytest.mark.asyncio
async def test_the_standing_board_reports_a_doubled_order(db):
    """The regression. Before ``check_duplicate_orders`` this returned nothing at all."""
    _account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Paracetamol")
    await _add_current_med(db, patient, "Paracetamol")

    flags = await SafetyService(db).chart_completeness_flags(patient.id)
    assert len(_dups(flags)) == 1


@pytest.mark.asyncio
async def test_one_order_leaves_the_standing_board_quiet(db):
    _account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Paracetamol")
    flags = await SafetyService(db).chart_completeness_flags(patient.id)
    assert _dups(flags) == []


@pytest.mark.asyncio
async def test_a_second_prescription_at_a_different_dose_reaches_the_board(auth_client):
    """End to end, through the only supported write path.

    This is how the two rows arise in production: the merge dedups on (generic name, dose), so
    the second prescription is not swallowed, and the patient ends up on both.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, _document(medications=("Glycomet 500mg OD",)), "visit1.pdf")
    await _ingest(auth_client, pid, _document(medications=("Glycomet 850mg OD",)), "visit2.pdf")

    board = (await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/flags")).json()
    dups = [
        f
        for f in board["flags"]
        if f["check_type"] == "duplicate_therapy" and f["details"].get("scope") == "chart"
    ]
    assert len(dups) == 1, board["flags"]
    assert dups[0]["details"]["order_count"] == 2
    assert dups[0]["is_hard_block"] is False


@pytest.mark.asyncio
async def test_the_two_rows_really_are_both_current(auth_client, db):
    """Pins the reachability the test above rests on, so that if the merge ever starts
    collapsing a dose change this file fails loudly rather than going quietly green."""
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, _document(medications=("Glycomet 500mg OD",)), "visit1.pdf")
    await _ingest(auth_client, pid, _document(medications=("Glycomet 850mg OD",)), "visit2.pdf")

    rows = (
        (
            await db.execute(
                select(MedicationEvent).where(
                    MedicationEvent.patient_id == uuid.UUID(pid),
                    MedicationEvent.is_current.is_(True),
                    MedicationEvent.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    vocab_ids = {r.drug_vocabulary_id for r in rows}
    # Explicitly not-None: two *unresolved* rows also collapse to a one-element set, and a row
    # that resolved to nothing never reaches ``current_meds`` at all — it is reported as an
    # unevaluated medication instead, which is a different flag and a different bug.
    assert vocab_ids != {None}, "the brand did not resolve; this fixture tests nothing"
    assert len(vocab_ids) == 1, "same drug, two live orders"
    assert {r.dose for r in rows} == {"500", "850"}, "the doses differ; that is why both landed"


@pytest.mark.asyncio
async def test_a_proposal_check_also_carries_the_chart_level_flag(auth_client):
    """``POST ../check`` appends every chart-level statement, so a doubled order is visible
    there too rather than only on the board."""
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, _document(medications=("Glycomet 500mg OD",)), "visit1.pdf")
    await _ingest(auth_client, pid, _document(medications=("Glycomet 850mg OD",)), "visit2.pdf")

    resp = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/check", json={"drug_name": "Metformin"}
    )
    assert resp.status_code == 200, resp.text
    dups = [
        f
        for f in resp.json()["flags"]
        if f["check_type"] == "duplicate_therapy" and f["details"].get("scope") == "chart"
    ]
    assert len(dups) == 1


@pytest.mark.asyncio
async def test_a_single_order_chart_stays_clean_end_to_end(auth_client):
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, _document(medications=("Glycomet 500mg OD",)), "visit1.pdf")

    board = (await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/flags")).json()
    assert [
        f
        for f in board["flags"]
        if f["check_type"] == "duplicate_therapy" and f["details"].get("scope") == "chart"
    ] == []


# --- What the chart-level placement protects ----------------------------------------------


@pytest.mark.asyncio
async def test_a_doubled_order_is_not_counted_twice_toward_a_cumulative_burden(db):
    """Why this is a chart-level check and not a loosening of the per-drug context.

    The obvious fix — stop dropping the drug's own second copy from its "everyone but me" set,
    so the pairwise check can see it — would also hand that copy to
    ``check_hepatotoxic_burden`` and ``check_bleeding_burden``, which count the drugs a patient
    is on. The drug would contribute twice to a burden it contributes to once, and those are
    the checks that escalate on a count. Keeping the duplicate detection out of the per-drug
    context is what keeps the burden checks honest.
    """
    _account, patient = await _account_and_patient(db)
    service = SafetyService(db)

    await _add_current_med(db, patient, "Paracetamol")
    single = await service.active_flags(account_id=_account.id, patient_id=patient.id)
    single_flags = sorted(f.summary for _v, fl in single for f in fl)

    service._facts.clear()
    await _add_current_med(db, patient, "Paracetamol")
    doubled = await service.active_flags(account_id=_account.id, patient_id=patient.id)
    doubled_flags = sorted(f.summary for _v, fl in doubled for f in fl)

    assert single_flags == doubled_flags, (
        "the per-drug board changed when the same drug was charted twice — a cumulative "
        "check is now counting one drug as two"
    )


# --- What a doubled row must not multiply -------------------------------------------------


@pytest.mark.asyncio
async def test_a_doubled_order_does_not_report_another_drugs_interaction_twice(db):
    """The second half of the same root cause.

    ``check_interactions`` walked ``current_meds`` row by row, so a patient charted with two
    live orders for aspirin had the aspirin/warfarin major interaction reported twice against
    warfarin — one rule, one pair, two rows on the screen whose value is that it is short
    enough to read carefully.
    """
    _account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Warfarin")
    await _add_current_med(db, patient, "Aspirin")

    before = await SafetyService(db).active_flags(account_id=_account.id, patient_id=patient.id)
    baseline = sorted(f.summary for _v, fl in before for f in fl)

    await _add_current_med(db, patient, "Aspirin")
    after = await SafetyService(db).active_flags(account_id=_account.id, patient_id=patient.id)
    doubled = sorted(f.summary for _v, fl in after for f in fl)

    assert doubled == baseline, "charting a drug twice changed the per-drug findings"


@pytest.mark.asyncio
async def test_a_doubled_order_is_still_only_one_duplicate_flag_on_a_proposal(db):
    """``POST ../check`` walks the same list. Proposing a third order of a drug already charted
    twice is one duplicate finding, not one per existing row."""
    _account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Metformin")
    await _add_current_med(db, patient, "Metformin")

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=_account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Metformin",
    )
    same_product = [
        f
        for f in flags
        if f.check_type == "duplicate_therapy" and f.details.get("match_type") == "same_product"
    ]
    # One from the pairwise check (the proposal duplicates an existing order) and one from the
    # chart-level check (the chart already carries two). Distinguished by ``scope``.
    assert len([f for f in same_product if f.details.get("scope") != "chart"]) == 1
    assert len([f for f in same_product if f.details.get("scope") == "chart"]) == 1


def test_distinct_current_meds_keeps_unidentified_molecules_apart():
    from app.core.safety import _distinct_current_meds

    meds = [
        DrugRef(reference_id="", generic_name="Clavulanic acid"),
        DrugRef(reference_id="", generic_name="Trimethoprim"),
        DrugRef(reference_id="MET-500", generic_name="Metformin"),
        DrugRef(reference_id="MET-500", generic_name="Metformin"),
    ]
    kept = _distinct_current_meds(meds)
    assert [m.generic_name for m in kept] == ["Clavulanic acid", "Trimethoprim", "Metformin"]
