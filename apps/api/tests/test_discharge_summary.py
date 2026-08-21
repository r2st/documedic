"""Discharge: the readiness gate, the confirmed discontinuations, and the chart write.

The narrative columns are the cheap half of this feature. Three things carry the design weight
and get almost all of the tests below:

* **Finalising writes to the chart.** This is the difference between a document and a record.
  Before it, a discharge summary listing five medicines sat next to a ``medication_events``
  table still carrying the admission's seven, and every safety check at the next visit ran
  against a list that had been wrong since the day the patient left. The tests in section 3
  assert on the chart afterwards, not on the response.

* **No discontinuation is written that the clinician has not named.**
  ``app.core.med_reconciliation`` is explicit that a ``stop`` disposition is a statement about
  two lists rather than an instruction: it means the take-home list does not carry a drug the
  chart calls current, which is an intended stop about half the time and a line somebody forgot
  to type the other half. Section 4 is the recompute-and-compare that closes that, in both
  directions — a drug that has *stopped* being absent since the preview is a stale confirmation
  exactly as much as a newly absent one is.

* **All of it or none of it.** A blocking readiness item refuses the whole finalisation with
  nothing charted. Half-charting a discharge leaves a record that reads as complete with one
  medicine quietly missing, which is strictly worse than the list the clinician started with.

Section 1 covers the pure rule set, which is where a bad refusal would come from, and section 6
covers Rule #4 microcopy and Rule #8 offline capability for the new module.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.clinical_language import has_certainty_language
from app.core.discharge import (
    DISCHARGE_STATUSES,
    REQUIRED_SECTIONS,
    ChartAction,
    DischargeFacts,
    assess_readiness,
    blocking_items,
    chart_actions,
    stop_labels,
)
from app.core.med_reconciliation import ReconciliationLine
from app.exceptions import (
    DischargeFinalizedError,
    DischargeNotReadyError,
    DischargeStopsUnconfirmedError,
    EncounterNotFoundError,
    NotFoundError,
    ValidationError,
)
from app.models.audit_log import AuditLog
from app.models.critical_lab_acknowledgement import CriticalLabAcknowledgement
from app.models.discharge_summary import (
    FINAL_STATUSES,
    FROZEN_ON_FINALIZE,
    DischargeSummary,
)
from app.models.drug_vocabulary import DrugVocabulary
from app.models.encounter import Encounter
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.discharge_service import DischargeService
from app.services.med_reconciliation_service import ProposedLine
from tests.conftest import create_patient

# No ``pytestmark = pytest.mark.asyncio``: ``asyncio_mode = "auto"`` in pyproject.toml already
# runs every async test, and a blanket mark additionally lands on the pure synchronous ones in
# sections 1 and 7, where pytest warns about it on every run.

NARRATIVE = {
    "admission_reason": "Community-acquired pneumonia, right lower lobe.",
    "hospital_course": (
        "Admitted 18/08 with fever and productive cough. Treated with IV antibiotics, "
        "stepped down to oral on day 3. Afebrile 48 hours before discharge."
    ),
    "discharge_diagnosis": "Community-acquired pneumonia, resolving.",
    "follow_up_instructions": "Chest radiograph in six weeks; review sooner if breathless.",
    "patient_instructions": "Complete the full course of tablets even once you feel better.",
}


# --- 1. The pure rule set ------------------------------------------------------------------


def test_a_clean_chart_produces_no_readiness_items():
    """Zero-count items are omitted, never listed as satisfied.

    A readiness list that always shows eight rows, three of them permanently "nothing to do", is
    one people learn to scroll past — and then the one that mattered is scrolled past
    identically. Same judgement the handover checklist makes.
    """
    items = assess_readiness(DischargeFacts(follow_up_booked=True, missing_sections=()))
    assert items == []


@pytest.mark.parametrize(
    ("facts", "key"),
    [
        (DischargeFacts(unacknowledged_critical_labs=1), "unacknowledged_critical_labs"),
        (DischargeFacts(hard_blocks=2), "active_hard_blocks"),
        (
            DischargeFacts(unresolved_discharge_medications=("Zzqxtrin",)),
            "unresolved_discharge_medications",
        ),
        (DischargeFacts(missing_sections=("discharge_diagnosis",)), "missing_sections"),
    ],
)
def test_the_four_blocking_states(facts, key):
    """Each is a state in which the document would assert something the record cannot support."""
    items = assess_readiness(facts)
    blocking = {item.key for item in blocking_items(items)}
    assert key in blocking


@pytest.mark.parametrize(
    ("facts", "key"),
    [
        (DischargeFacts(high_risk_omissions=("Levothyroxine",)), "high_risk_omissions"),
        (
            DischargeFacts(unresolved_charted_medications=("Squiggle 5mg",)),
            "unresolved_charted_medications",
        ),
        (DischargeFacts(), "no_follow_up_arranged"),
        (DischargeFacts(documents_needing_confirmation=3), "documents_needing_confirmation"),
    ],
)
def test_the_advisory_states_do_not_block(facts, key):
    """Reported, counted, and proceeded past by a clinician who has read them.

    Deliberately conservative about what it refuses. A rule that blocks a discharge over
    something the clinician cannot fix from this screen does not make the discharge safer; it
    makes it happen on paper instead.
    """
    items = assess_readiness(facts)
    assert key in {item.key for item in items}
    assert key not in {item.key for item in blocking_items(items)}


def test_either_kind_of_follow_up_satisfies_the_advisory():
    booked = assess_readiness(DischargeFacts(follow_up_booked=True))
    written = assess_readiness(DischargeFacts(follow_up_instructions=True))
    for items in (booked, written):
        assert "no_follow_up_arranged" not in {item.key for item in items}


def test_blocking_items_sort_above_advisory_ones():
    """The list is read top-down and what stops a discharge belongs at the top of it."""
    items = assess_readiness(
        DischargeFacts(
            documents_needing_confirmation=1,
            high_risk_omissions=("Prednisolone",),
            hard_blocks=1,
            unacknowledged_critical_labs=1,
        )
    )
    severities = [item.severity for item in items]
    assert severities == sorted(severities, key=lambda s: {"blocking": 0, "advisory": 1}[s])
    # And within blocking, a fixed order — two runs over one chart produce one document.
    assert [item.key for item in items][:2] == [
        "unacknowledged_critical_labs",
        "active_hard_blocks",
    ]


def test_the_assessment_is_deterministic():
    facts = DischargeFacts(hard_blocks=1, high_risk_omissions=("Warfarin",))
    assert assess_readiness(facts) == assess_readiness(facts)


def _line(disposition: str, label: str, **kw) -> ReconciliationLine:
    return ReconciliationLine(
        disposition=disposition, label=label, summary=f"{label}: {disposition}", **kw
    )


def test_dispositions_become_chart_actions_in_a_fixed_order():
    """Starts, then changes, then stops, then the continuations that write nothing.

    Not cosmetic: the order the events are written in is the order they appear in the chart's
    own history, and a reader walking the discharge afterwards should meet what the patient is
    now on before what they came off.
    """
    actions = chart_actions(
        [
            _line("stop", "Warfarin", charted_dose="5"),
            _line("continue", "Metformin"),
            _line("start", "Azithromycin", proposed_dose="500"),
            _line("dose_change", "Levothyroxine", proposed_dose="125", charted_dose="100"),
        ]
    )
    assert [a.kind for a in actions] == ["start", "change", "stop", "continue"]


def test_a_continuation_writes_nothing():
    """The chart already carries the drug at that dose.

    A second identical row would surface in the duplicate-therapy check as the patient being on
    it twice — a fabricated finding on a chart nobody changed.
    """
    (action,) = chart_actions([_line("continue", "Metformin")])
    assert action.writes_to_chart is False


def test_an_unreconciled_line_produces_no_action():
    """A name nothing could identify was compared against nothing, so it charts nothing.

    Unreachable through the API — the readiness gate refuses the finalisation on any unresolved
    name — and asserted anyway, because the pure function has to be safe on its own terms.
    """
    assert chart_actions([_line("unresolved_proposed", "Zzqxtrin")]) == []
    assert chart_actions([_line("unresolved_charted", "Squiggle")]) == []


def test_a_change_carries_the_dose_it_moved_from():
    """The previous dose is gone from the chart the moment the row is retired."""
    (action,) = chart_actions(
        [_line("dose_change", "Levothyroxine", proposed_dose="125", charted_dose="100")]
    )
    assert (action.dose, action.previous_dose) == ("125", "100")


def test_stop_labels_are_folded_for_comparison():
    """The confirmation arrives as text a client echoed back; the match must not turn on case."""
    actions = [ChartAction(kind="stop", label="  WARFARIN "), ChartAction(kind="start", label="X")]
    assert stop_labels(actions) == ("warfarin",)


# --- 2. Drafting ---------------------------------------------------------------------------


async def _seeded(db) -> tuple[Account, Patient]:
    account = Account(email=f"discharge-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Discharge Patient",
        sex="female",
        date_of_birth=datetime(1957, 4, 11).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _vocab(db, generic: str) -> DrugVocabulary:
    row = (
        (await db.execute(select(DrugVocabulary).where(DrugVocabulary.generic_name.ilike(generic))))
        .scalars()
        .first()
    )
    assert row is not None, f"seed data is missing {generic}"
    return row


async def _chart(db, patient: Patient, generic: str, **columns) -> MedicationEvent:
    row = await _vocab(db, generic)
    event = MedicationEvent(
        patient_id=patient.id,
        drug_vocabulary_id=row.id,
        generic_name=row.generic_name,
        event_type="continue",
        is_current=True,
        **columns,
    )
    db.add(event)
    await db.flush()
    return event


async def _draft(db, account, patient, *, meds: list[ProposedLine], **overrides):
    sections = {**NARRATIVE, **overrides}
    encounter_id = sections.pop("encounter_id", None)
    return await DischargeService(db).create(
        account_id=account.id,
        patient_id=patient.id,
        encounter_id=encounter_id,
        medications=meds,
        **sections,
    )


async def test_a_draft_requires_nothing(db):
    """A summary that could not be saved half-written is one written in a single pass at 8pm."""
    account, patient = await _seeded(db)
    summary = await DischargeService(db).create(
        account_id=account.id,
        patient_id=patient.id,
        encounter_id=None,
        medications=[],
    )
    assert summary.status == "draft"
    assert summary.discharge_diagnosis is None
    assert summary.discharge_medications == []


async def test_a_draft_can_be_edited_and_the_list_is_replaced_wholesale(db):
    """A partial update of a medication list has no safe reading.

    "These three changed" leaves every other line ambiguous between unchanged and removed, and
    completeness is the entire value of the list.
    """
    account, patient = await _seeded(db)
    summary = await _draft(db, account, patient, meds=[ProposedLine(name="Metformin")])
    updated = await DischargeService(db).update(
        account_id=account.id,
        patient_id=patient.id,
        summary_id=summary.id,
        encounter_id=None,
        medications=[ProposedLine(name="Azithromycin", dose="500", dose_unit="mg")],
    )
    assert [row["name"] for row in updated.discharge_medications] == ["Azithromycin"]


async def test_an_encounter_from_another_chart_is_refused(db):
    """The admission this closes has to be this patient's admission."""
    account, patient = await _seeded(db)
    _other_account, other_patient = await _seeded(db)
    encounter = Encounter(
        patient_id=other_patient.id,
        encounter_type="inpatient",
        status="draft",
        encounter_date=datetime(2026, 8, 18).date(),
    )
    db.add(encounter)
    await db.flush()

    with pytest.raises(EncounterNotFoundError):
        await _draft(
            db,
            account,
            patient,
            meds=[ProposedLine(name="Metformin")],
            encounter_id=encounter.id,
        )


async def test_another_accounts_chart_cannot_be_discharged(db):
    account, _patient = await _seeded(db)
    _other, other_patient = await _seeded(db)
    from app.exceptions import PatientNotFoundError

    with pytest.raises(PatientNotFoundError):
        await DischargeService(db).create(
            account_id=account.id,
            patient_id=other_patient.id,
            encounter_id=None,
            medications=[],
        )


async def test_a_summary_on_another_chart_is_not_found(db):
    account, patient = await _seeded(db)
    _other_account, other_patient = await _seeded(db)
    summary = await _draft(db, account, patient, meds=[ProposedLine(name="Metformin")])
    with pytest.raises(NotFoundError):
        await DischargeService(db).get(other_patient.id, summary.id)


# --- 3. The chart write, which is the point of the feature ---------------------------------


async def _finalize(db, account, patient, summary, *, stops: list[str] | None = None, **kw):
    return await DischargeService(db).finalize(
        account_id=account.id,
        patient_id=patient.id,
        summary_id=summary.id,
        finalized_by=kw.pop("finalized_by", "Dr A Rao"),
        confirmed_stops=stops if stops is not None else [],
        supersedes_id=kw.pop("supersedes_id", None),
        correction_reason=kw.pop("correction_reason", None),
    )


async def _current(db, patient: Patient) -> dict[str, str | None]:
    rows = (
        (
            await db.execute(
                select(MedicationEvent).where(
                    MedicationEvent.patient_id == patient.id,
                    MedicationEvent.is_deleted.is_(False),
                    MedicationEvent.is_current.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.generic_name: row.dose for row in rows}


async def test_finalising_charts_the_take_home_list(db):
    """The defect the whole feature is about.

    Before this, the patient went home on the new list and ``medication_events`` went on
    carrying the admission's — so every safety check at the next visit ran against a medication
    list that had been wrong since the day they left.
    """
    account, patient = await _seeded(db)
    await _chart(db, patient, "Metformin", dose="500")
    summary = await _draft(
        db,
        account,
        patient,
        meds=[
            ProposedLine(name="Metformin", dose="500"),
            ProposedLine(name="Azithromycin", dose="500"),
        ],
    )

    finalized, assessment = await _finalize(db, account, patient, summary)

    assert finalized.status == "finalized"
    assert finalized.finalized_by == "Dr A Rao"
    current = await _current(db, patient)
    assert "Azithromycin" in current, "the new drug never reached the chart"
    assert "Metformin" in current
    # Exactly one event written: the start. The continuation writes nothing.
    assert len(finalized.medication_event_ids) == 1
    assert [a.kind for a in assessment.actions].count("continue") == 1


async def test_a_confirmed_discontinuation_takes_the_patient_off_the_drug(db):
    """The stop row and the retirement, together.

    Without the retirement the stop lands beside the row it contradicts: the earlier "continue
    Warfarin" is still ``is_current``, so the safety engine, the records list and every count
    derived from them still have the patient on it. Exactly the failure
    ``GraphService._retire_current`` exists to prevent on the extraction path.
    """
    account, patient = await _seeded(db)
    await _chart(db, patient, "Metformin", dose="500")
    await _chart(db, patient, "Warfarin", dose="5")
    summary = await _draft(db, account, patient, meds=[ProposedLine(name="Metformin", dose="500")])

    _finalized, assessment = await _finalize(db, account, patient, summary, stops=["Warfarin"])

    current = await _current(db, patient)
    assert "Warfarin" not in current, "the discontinued drug is still current on the chart"
    assert "Metformin" in current
    stop_rows = (
        (
            await db.execute(
                select(MedicationEvent).where(
                    MedicationEvent.patient_id == patient.id,
                    MedicationEvent.event_type == "stop",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(stop_rows) == 1
    # ``is_current = event_type != "stop"``, the rule the whole chart is driven by. A stop row
    # claiming to be current is the disagreement migration 0022 was written to end.
    assert stop_rows[0].is_current is False
    assert assessment.stops == ("warfarin",)


async def test_a_dose_change_retires_the_old_row(db):
    """Otherwise the chart holds both doses and the safety engine reads the patient as on two."""
    account, patient = await _seeded(db)
    await _chart(db, patient, "Levothyroxine", dose="100")
    summary = await _draft(
        db, account, patient, meds=[ProposedLine(name="Levothyroxine", dose="125")]
    )

    await _finalize(db, account, patient, summary)

    current = await _current(db, patient)
    assert current == {"Levothyroxine": "125"}


async def test_charted_events_carry_the_encounter_and_the_prescriber(db):
    """The chart change and the document that caused it point at each other in both directions."""
    account, patient = await _seeded(db)
    encounter = Encounter(
        patient_id=patient.id,
        encounter_type="inpatient",
        status="draft",
        encounter_date=datetime(2026, 8, 18).date(),
    )
    db.add(encounter)
    await db.flush()
    summary = await _draft(
        db,
        account,
        patient,
        meds=[ProposedLine(name="Azithromycin", dose="500")],
        encounter_id=encounter.id,
    )

    finalized, _assessment = await _finalize(db, account, patient, summary)

    (event_id,) = finalized.medication_event_ids
    event = await db.get(MedicationEvent, uuid.UUID(event_id))
    assert event is not None
    assert event.encounter_id == encounter.id
    assert event.prescriber_name == "Dr A Rao"
    assert event.clinician_confirmed is True


async def test_the_generic_name_comes_from_the_vocabulary_not_the_typed_label(db):
    """A discharge list spelled differently must not introduce a second spelling into the chart.

    An Indian brand name is the ordinary way a take-home list is written, and the vocabulary is
    the authority on what molecule it is.
    """
    account, patient = await _seeded(db)
    brand = (
        (
            await db.execute(
                select(DrugVocabulary).where(
                    DrugVocabulary.generic_name.ilike("Paracetamol"),
                    DrugVocabulary.brand_name.isnot(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if brand is None:  # pragma: no cover - seed data always carries one
        pytest.skip("seed vocabulary carries no branded paracetamol row")

    summary = await _draft(db, account, patient, meds=[ProposedLine(name=brand.brand_name)])
    finalized, _assessment = await _finalize(db, account, patient, summary)

    (event_id,) = finalized.medication_event_ids
    event = await db.get(MedicationEvent, uuid.UUID(event_id))
    assert event is not None
    assert event.generic_name == brand.generic_name


# --- 4. Confirmed discontinuations ---------------------------------------------------------


async def test_an_unconfirmed_discontinuation_refuses_the_whole_finalisation(db):
    """And charts nothing at all, not "everything except the stop".

    A ``stop`` disposition is an intended discontinuation about half the time and a line
    somebody forgot to type the other half. Writing the ones nobody named is how a transcription
    slip becomes a discontinuation of record.
    """
    account, patient = await _seeded(db)
    await _chart(db, patient, "Warfarin", dose="5")
    summary = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])

    with pytest.raises(DischargeStopsUnconfirmedError):
        await _finalize(db, account, patient, summary, stops=[])

    assert summary.status == "draft"
    assert "Warfarin" in await _current(db, patient)
    assert "Azithromycin" not in await _current(db, patient)


async def test_confirming_a_discontinuation_that_is_no_longer_one_is_refused(db):
    """The stale confirmation, in the other direction.

    Somebody added the drug back to the list between the preview and the finalisation. The
    clinician confirmed a picture that is no longer the current one, exactly as much as if a new
    omission had appeared, and accepting the superset would wave a stale confirmation through
    whenever the list happened to grow.
    """
    account, patient = await _seeded(db)
    await _chart(db, patient, "Warfarin", dose="5")
    summary = await _draft(
        db,
        account,
        patient,
        meds=[ProposedLine(name="Warfarin", dose="5"), ProposedLine(name="Azithromycin")],
    )

    with pytest.raises(DischargeStopsUnconfirmedError):
        await _finalize(db, account, patient, summary, stops=["Warfarin"])


async def test_confirmation_is_case_insensitive(db):
    account, patient = await _seeded(db)
    await _chart(db, patient, "Warfarin", dose="5")
    summary = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])

    finalized, _assessment = await _finalize(db, account, patient, summary, stops=["  wArFaRiN "])
    assert finalized.status == "finalized"
    assert finalized.confirmed_stops == ["warfarin"]


# --- 5. The readiness gate -----------------------------------------------------------------


async def _panic_potassium(db, patient: Patient) -> LabResult:
    lab = LabResult(
        patient_id=patient.id,
        marker_name="Potassium",
        value_numeric=7.1,
        unit="mmol/L",
        sample_date=datetime.now(UTC),
    )
    db.add(lab)
    await db.flush()
    return lab


async def test_an_unacknowledged_panic_value_refuses_the_discharge(db):
    """A discharge summary written over a result nobody acted on states that the episode ended."""
    account, patient = await _seeded(db)
    await _panic_potassium(db, patient)
    summary = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])

    with pytest.raises(DischargeNotReadyError) as excinfo:
        await _finalize(db, account, patient, summary)

    assert "unacknowledged_critical_labs" in str(excinfo.value.detail)
    assert summary.status == "draft"
    assert await _current(db, patient) == {}, "nothing may be charted by a refused finalisation"


async def test_acknowledging_the_value_clears_the_refusal(db):
    account, patient = await _seeded(db)
    lab = await _panic_potassium(db, patient)
    db.add(
        CriticalLabAcknowledgement(
            account_id=account.id,
            patient_id=patient.id,
            lab_result_id=lab.id,
            marker_name="Potassium",
            value=7.1,
            unit="mmol/L",
            severity="panic",
            acknowledged_by="Dr A Rao",
        )
    )
    await db.flush()
    summary = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])

    finalized, _assessment = await _finalize(db, account, patient, summary)
    assert finalized.status == "finalized"


async def test_a_missing_required_section_refuses_the_discharge(db):
    """The two things the next clinician opens the document to find."""
    account, patient = await _seeded(db)
    summary = await _draft(
        db,
        account,
        patient,
        meds=[ProposedLine(name="Azithromycin")],
        discharge_diagnosis=None,
    )

    with pytest.raises(DischargeNotReadyError) as excinfo:
        await _finalize(db, account, patient, summary)
    assert "missing_sections" in str(excinfo.value.detail)


async def test_a_whitespace_only_section_does_not_satisfy_the_requirement(db):
    """A section that reads as filled in but holds nothing is worse than a visibly empty one."""
    account, patient = await _seeded(db)
    summary = await _draft(
        db, account, patient, meds=[ProposedLine(name="Azithromycin")], hospital_course="   "
    )
    with pytest.raises(DischargeNotReadyError):
        await _finalize(db, account, patient, summary)


async def test_an_unidentifiable_take_home_drug_refuses_the_discharge(db):
    """A medicine checked against nothing, inside a document that reads as reconciled.

    No allergy check, no interaction check, no dose range — and the summary would still say the
    list was reconciled.
    """
    account, patient = await _seeded(db)
    summary = await _draft(
        db,
        account,
        patient,
        meds=[ProposedLine(name="Azithromycin"), ProposedLine(name="Zzqxtrin 40mg")],
    )

    with pytest.raises(DischargeNotReadyError) as excinfo:
        await _finalize(db, account, patient, summary)
    assert "unresolved_discharge_medications" in str(excinfo.value.detail)


async def test_an_empty_take_home_list_is_refused_rather_than_answered(db):
    """An empty list against a non-empty chart is a request to stop everything.

    It would come back as every current medication reported as an omission, and a screenful of
    high-risk-omission flags produced by a client that failed to send its list is how a
    clinician learns to dismiss them. ``MedReconciliationService`` refuses it for the same
    reason.
    """
    account, patient = await _seeded(db)
    await _chart(db, patient, "Warfarin", dose="5")
    summary = await _draft(db, account, patient, meds=[])

    with pytest.raises(ValidationError):
        await DischargeService(db).assess(
            account_id=account.id, patient_id=patient.id, summary=summary
        )


async def test_a_future_appointment_satisfies_the_follow_up_advisory(db):
    from app.models.appointment import Appointment

    account, patient = await _seeded(db)
    db.add(
        Appointment(
            account_id=account.id,
            patient_id=patient.id,
            provider_name="Dr A Rao",
            starts_at=datetime.now(UTC) + timedelta(days=42),
            ends_at=datetime.now(UTC) + timedelta(days=42, minutes=15),
            status="scheduled",
        )
    )
    await db.flush()
    summary = await _draft(
        db,
        account,
        patient,
        meds=[ProposedLine(name="Azithromycin")],
        follow_up_instructions=None,
    )
    _summary, assessment = await DischargeService(db).preview(
        account_id=account.id, patient_id=patient.id, summary_id=summary.id
    )
    assert "no_follow_up_arranged" not in {item.key for item in assessment.readiness}


async def test_a_cancelled_appointment_arranges_nothing(db):
    """Counting it would report a follow-up that does not exist, which is the claim being made."""
    from app.models.appointment import Appointment

    account, patient = await _seeded(db)
    db.add(
        Appointment(
            account_id=account.id,
            patient_id=patient.id,
            provider_name="Dr A Rao",
            starts_at=datetime.now(UTC) + timedelta(days=42),
            ends_at=datetime.now(UTC) + timedelta(days=42, minutes=15),
            status="cancelled",
            cancelled_at=datetime.now(UTC),
            cancellation_reason="Patient could not attend",
        )
    )
    await db.flush()
    summary = await _draft(
        db,
        account,
        patient,
        meds=[ProposedLine(name="Azithromycin")],
        follow_up_instructions=None,
    )
    _summary, assessment = await DischargeService(db).preview(
        account_id=account.id, patient_id=patient.id, summary_id=summary.id
    )
    assert "no_follow_up_arranged" in {item.key for item in assessment.readiness}


async def test_an_advisory_item_is_recorded_on_the_finalised_summary(db):
    """What the clinician read and proceeded past is exactly what a later review asks about."""
    account, patient = await _seeded(db)
    summary = await _draft(
        db,
        account,
        patient,
        meds=[ProposedLine(name="Azithromycin")],
        follow_up_instructions=None,
    )
    finalized, _assessment = await _finalize(db, account, patient, summary)
    assert "no_follow_up_arranged" in {item["key"] for item in finalized.readiness}


# --- 6. Immutability, corrections and the trail --------------------------------------------


async def test_a_finalised_summary_cannot_be_edited_or_finalised_again(db):
    """A document that can be rewritten afterwards is not evidence of what the patient was told.

    It would also no longer describe the medication events it created. Refused here and by
    ``trg_discharge_summaries_final_frozen`` on PostgreSQL (migration 0044).
    """
    account, patient = await _seeded(db)
    summary = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])
    await _finalize(db, account, patient, summary)

    with pytest.raises(DischargeFinalizedError):
        await DischargeService(db).update(
            account_id=account.id,
            patient_id=patient.id,
            summary_id=summary.id,
            encounter_id=None,
            medications=None,
            discharge_diagnosis="Something else entirely",
        )
    with pytest.raises(DischargeFinalizedError):
        await _finalize(db, account, patient, summary)


def test_every_clinical_column_is_frozen_by_finalisation():
    """The freeze list is the model's, so a column added without a decision is a visible gap.

    Deliberately absent: ``status`` (there is nowhere further for it to go, and the trigger
    refuses a move backwards separately), ``updated_at``, and the soft-delete pair — withdrawing
    a chart has to keep working on one holding finalised discharges.
    """
    columns = {c.name for c in DischargeSummary.__table__.columns}
    unfrozen = columns - set(FROZEN_ON_FINALIZE)
    assert unfrozen == {
        "id",
        "account_id",
        "status",
        "created_at",
        "updated_at",
        "is_deleted",
        "deleted_at",
    }


async def test_a_correction_names_what_it_supersedes_and_why(db):
    account, patient = await _seeded(db)
    first = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])
    finalized, _assessment = await _finalize(db, account, patient, first)

    second = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])
    corrected, _a = await _finalize(
        db,
        account,
        patient,
        second,
        supersedes_id=finalized.id,
        correction_reason="The antibiotic course length was wrong.",
    )
    assert corrected.supersedes_id == finalized.id


async def test_a_correction_pointer_without_a_reason_is_refused(db):
    """A replacement nobody had to justify. And a reason pointing at nothing is a note."""
    account, patient = await _seeded(db)
    first = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])
    finalized, _assessment = await _finalize(db, account, patient, first)
    second = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])

    with pytest.raises(ValidationError):
        await _finalize(db, account, patient, second, supersedes_id=finalized.id)
    with pytest.raises(ValidationError):
        await _finalize(db, account, patient, second, correction_reason="Because.")


async def test_a_draft_cannot_be_superseded(db):
    """Only a finalised summary can be corrected. A draft is edited."""
    account, patient = await _seeded(db)
    draft = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])
    second = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])
    with pytest.raises(ValidationError):
        await _finalize(db, account, patient, second, supersedes_id=draft.id, correction_reason="x")


async def test_finalising_writes_the_entry_that_says_those_events_were_one_decision(db):
    """Afterwards the medication events are indistinguishable from lines typed in one at a time.

    Nothing else in the record can say that those starts and stops were one clinician's decision
    at one transition of care.
    """
    account, patient = await _seeded(db)
    await _chart(db, patient, "Warfarin", dose="5")
    summary = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])
    await _finalize(db, account, patient, summary, stops=["Warfarin"])

    entry = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "discharge_summary_finalized")))
        .scalars()
        .one()
    )
    assert entry.entity_id == summary.id
    assert entry.payload["finalized_by"] == "Dr A Rao"
    assert entry.payload["stops_confirmed"] == 1
    assert entry.payload["events_written"] == 2
    assert entry.payload["dispositions"] == {"start": 1, "stop": 1}


async def test_the_snapshot_is_stored_not_recomputed(db):
    """The record's job is to say what was true when the patient went home.

    A table that re-derived itself on read would show today's chart, which is a different and
    occasionally contradictory document.
    """
    account, patient = await _seeded(db)
    await _chart(db, patient, "Metformin", dose="500")
    summary = await _draft(db, account, patient, meds=[ProposedLine(name="Metformin", dose="500")])
    finalized, _assessment = await _finalize(db, account, patient, summary)

    stored = finalized.reconciliation
    assert stored["charted_count"] == 1
    assert [line["disposition"] for line in stored["lines"]] == ["continue"]

    # The chart moves on; the snapshot does not.
    await _chart(db, patient, "Warfarin", dose="5")
    reread = await DischargeService(db).get(patient.id, summary.id)
    assert reread.reconciliation == stored


async def test_the_snapshot_holds_no_second_copy_of_a_safety_finding(db):
    """Those live on ``drug_safety_checks`` rows with their own ids and their own override route.

    A copy frozen into a JSON column would never reflect an override recorded against the real
    one, and the two would then disagree about whether a hard block still stood.
    """
    account, patient = await _seeded(db)
    summary = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])
    finalized, _assessment = await _finalize(db, account, patient, summary)
    assert "safety_flags" not in finalized.reconciliation
    assert "safety_findings" not in finalized.reconciliation


# --- 7. Rule #4 microcopy and Rule #8 offline capability -----------------------------------


def test_no_readiness_microcopy_asserts_or_instructs():
    """Critical Safety Rule #4, over every string this module can emit.

    Each line states what the record shows and leaves the decision where it belongs. Nothing
    here tells a clinician to do anything.
    """
    facts = DischargeFacts(
        unacknowledged_critical_labs=2,
        hard_blocks=1,
        unresolved_discharge_medications=("Zzqxtrin",),
        unresolved_charted_medications=("Squiggle",),
        high_risk_omissions=("Prednisolone",),
        documents_needing_confirmation=1,
        missing_sections=REQUIRED_SECTIONS,
    )
    for item in assess_readiness(facts):
        assert not has_certainty_language(item.summary), item.summary


def test_singular_and_plural_microcopy_both_read():
    """One panic value is "1 critical laboratory value", not "1 critical laboratory values"."""
    one = assess_readiness(DischargeFacts(unacknowledged_critical_labs=1))[0].summary
    many = assess_readiness(DischargeFacts(unacknowledged_critical_labs=3))[0].summary
    assert "1 critical laboratory value " in one
    assert "3 critical laboratory values " in many


def test_the_readiness_rules_reach_no_clock_no_database_and_no_provider():
    """Critical Safety Rule #8, restated where the module lives.

    ``test_offline_engine_purity`` pins it as a property of the import graph and is the real
    guarantee; this is the local statement of why ``app.core.discharge`` is on that list.
    Deciding whether it is safe to send a patient home is the last thing that should wait on an
    LLM provider being reachable.
    """
    from pathlib import Path

    import app.core.discharge as module

    assert module.__file__ is not None
    source = Path(module.__file__).read_text()
    for call in ("datetime.now(", "date.today(", "time.time("):
        assert call not in source


def test_the_uniqueness_predicate_reads_the_same_in_both_dialects():
    """``uq_discharge_summaries_one_final_per_encounter`` is spelled twice, held together here.

    ``text()`` must take a literal string everywhere in this codebase
    (``test_sql_injection_surface``), so the predicate cannot interpolate ``FINAL_STATUSES`` and
    is written out per dialect instead. That leaves two copies that can drift — and the SQLite
    one is not decoration: without it the ``postgresql_`` prefix means the clause is simply
    dropped on SQLite and the index is built unpartitioned, which would refuse a second draft
    against one admission and refuse every outpatient summary after the first, since those all
    carry a NULL encounter_id.
    """
    index = next(
        i
        for i in DischargeSummary.__table__.indexes
        if i.name == "uq_discharge_summaries_one_final_per_encounter"
    )
    sqlite_where = str(index.dialect_options["sqlite"]["where"])
    postgres_where = str(index.dialect_options["postgresql"]["where"])
    for status in FINAL_STATUSES:
        assert f"'{status}'" in sqlite_where
        assert f"'{status}'" in postgres_where
    # The same clause in the two dialects' spellings of false, and nothing else different.
    assert sqlite_where.replace("is_deleted = 0", "X") == postgres_where.replace(
        "is_deleted = false", "X"
    )
    assert "encounter_id IS NOT NULL" in sqlite_where


async def test_several_drafts_may_exist_against_one_admission(db):
    """Only the *finalised* account is unique. Several people may start writing one."""
    account, patient = await _seeded(db)
    encounter = Encounter(
        patient_id=patient.id,
        encounter_type="inpatient",
        status="draft",
        encounter_date=datetime(2026, 8, 18).date(),
    )
    db.add(encounter)
    await db.flush()

    for _ in range(3):
        await _draft(
            db,
            account,
            patient,
            meds=[ProposedLine(name="Azithromycin")],
            encounter_id=encounter.id,
        )
    assert (
        len(
            await DischargeService(db).list_for_patient(
                account_id=account.id, patient_id=patient.id
            )
        )
        == 3
    )


async def test_two_outpatient_summaries_are_not_a_uniqueness_collision(db):
    """Every outpatient summary carries a NULL encounter_id.

    An unpartitioned unique index over that column would let the first one through and refuse
    every summary the practice wrote afterwards — a whole-account outage produced by an index
    predicate that was dropped on one dialect.
    """
    account, patient = await _seeded(db)
    first = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])
    await _finalize(db, account, patient, first)
    second = await _draft(db, account, patient, meds=[ProposedLine(name="Azithromycin")])
    finalized, _assessment = await _finalize(db, account, patient, second)
    assert finalized.status == "finalized"


def test_the_status_vocabulary_is_defined_once():
    """The check constraint, the service's transitions and the schema enum read one list."""
    assert DISCHARGE_STATUSES == ("draft", "finalized")


# --- 8. The routes -------------------------------------------------------------------------


async def test_the_route_walks_a_discharge_from_draft_to_chart(auth_client):
    patient = await create_patient(auth_client)
    base = f"/api/v1/patients/{patient['id']}/discharge-summaries"

    created = await auth_client.post(
        base, json={**NARRATIVE, "medications": [{"name": "Metformin"}]}
    )
    assert created.status_code == 201, created.text
    summary_id = created.json()["id"]

    preview = await auth_client.post(f"{base}/{summary_id}/preview")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["is_ready"] is True
    assert body["offline_capable"] is True
    assert [a["kind"] for a in body["chart_actions"]] == ["start"]

    final = await auth_client.post(
        f"{base}/{summary_id}/finalize",
        json={
            "finalized_by": "Dr A Rao",
            "confirmed_stops": body["stops_requiring_confirmation"],
        },
    )
    assert final.status_code == 200, final.text

    stored = await auth_client.get(f"{base}/{summary_id}")
    assert stored.json()["status"] == "finalized"
    assert len(stored.json()["medication_event_ids"]) == 1

    listing = await auth_client.get(base)
    assert [s["id"] for s in listing.json()["summaries"]] == [summary_id]


async def test_the_route_refuses_a_finalisation_that_is_not_ready(auth_client):
    patient = await create_patient(auth_client)
    base = f"/api/v1/patients/{patient['id']}/discharge-summaries"
    created = await auth_client.post(
        base,
        json={
            **{k: v for k, v in NARRATIVE.items() if k != "discharge_diagnosis"},
            "medications": [{"name": "Metformin"}],
        },
    )
    summary_id = created.json()["id"]

    resp = await auth_client.post(
        f"{base}/{summary_id}/finalize", json={"finalized_by": "Dr A Rao"}
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "discharge_not_ready"


async def test_the_route_refuses_an_edit_after_finalisation(auth_client):
    patient = await create_patient(auth_client)
    base = f"/api/v1/patients/{patient['id']}/discharge-summaries"
    created = await auth_client.post(
        base, json={**NARRATIVE, "medications": [{"name": "Metformin"}]}
    )
    summary_id = created.json()["id"]
    await auth_client.post(f"{base}/{summary_id}/finalize", json={"finalized_by": "Dr A Rao"})

    resp = await auth_client.patch(
        f"{base}/{summary_id}", json={"discharge_diagnosis": "Rewritten after the fact."}
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "discharge_finalized"


async def test_the_route_refuses_an_over_long_medication_list(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/discharge-summaries",
        json={**NARRATIVE, "medications": [{"name": f"Drug {n}"} for n in range(60)]},
    )
    assert resp.status_code == 422


async def test_the_read_route_refuses_another_accounts_chart(auth_client, second_auth_client):
    """The tenancy check runs before the summary is looked up.

    Otherwise a caller could tell a summary id that does not exist from one on a chart they do
    not own — by the response if the two 404s differ, and by the timing even if they do not.
    """
    patient = await create_patient(auth_client)
    created = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/discharge-summaries",
        json={**NARRATIVE, "medications": [{"name": "Metformin"}]},
    )
    summary_id = created.json()["id"]

    resp = await second_auth_client.get(
        f"/api/v1/patients/{patient['id']}/discharge-summaries/{summary_id}"
    )
    assert resp.status_code == 404
