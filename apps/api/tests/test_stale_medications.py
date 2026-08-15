"""A medication list nothing ever ages, presented as though it were today's.

``is_current`` is written once, by the merge that created the row, and only a later ``stop``
line ever clears it. Nothing else in this system — not the safety engine, not the record read,
not the agent snapshot — looks at how old a medication row is. So a five-day antibiotic course
lifted off a 2019 prescription is still ``is_current`` in 2026: it sits in ``current_meds``,
every interaction and duplicate-therapy rule runs against it as live therapy, and the record
screen listed it under "Medications" showing generic name, dose and frequency — and no date at
all. A clinician could not tell from that screen whether the line was charted last week or
before the pandemic.

The fix is deliberately not the intuitive one. *Expiring* aged rows would silently shrink the
list every hard block is computed from — an anticoagulant last charted eight months ago would
stop conflicting with a new NSAID and the screen would go quiet, which is the fail-open shape
this engine has been corrected for repeatedly. Patients on long-term therapy here routinely go a
year between documented prescriptions, so "not documented recently" is not evidence of "not
taking". Every row stays, every rule still runs, and what is added is the sentence that makes
the result readable: this evaluation assumed a list last updated a long time ago.

These tests pin both halves — that the flag appears, and that the drug it is about is still
being checked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.safety import (
    _MEDICATION_STALE_AFTER_DAYS,
    ChartedMedication,
    SafetyContext,
    check_stale_medications,
)
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

TODAY = datetime.now(UTC).date()


def _days_ago(days: int) -> date:
    return TODAY - timedelta(days=days)


async def _patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"stale-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Stale Chart Patient",
        sex="male",
        date_of_birth=date(1968, 4, 2),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _chart(
    db,
    patient: Patient,
    generic: str,
    *,
    charted_on: date | None,
    is_current: bool = True,
) -> MedicationEvent:
    """One medication row, written straight in so its date is exactly what the test says."""
    med = MedicationEvent(
        patient_id=patient.id,
        generic_name=generic,
        event_type="continue",
        event_date=charted_on,
        is_current=is_current,
    )
    db.add(med)
    await db.flush()
    return med


async def _notes(db, patient: Patient) -> dict[str, dict]:
    flags = await SafetyService(db).chart_completeness_flags(patient.id)
    return {flag.check_type: flag.details for flag in flags}


# --- The check itself, on hand-built contexts ---------------------------------------------------


def _ctx(*meds: ChartedMedication) -> SafetyContext:
    return SafetyContext(charted_current_meds=list(meds))


def test_a_chart_documented_this_month_says_nothing():
    """The quiet case has to stay quiet, or the flag is on every chart and means nothing."""
    ctx = _ctx(ChartedMedication("Metformin", _days_ago(9), 9))
    assert check_stale_medications(ctx) == []


def test_a_drug_last_documented_years_ago_is_named_with_how_long_ago():
    flags = check_stale_medications(_ctx(ChartedMedication("Amoxicillin", date(2019, 6, 1), 2632)))
    assert len(flags) == 1
    flag = flags[0]
    assert flag.check_type == "stale_medication"
    assert "Amoxicillin" in flag.summary
    assert "over 7 years ago" in flag.summary
    assert flag.details["stale_medications"] == [
        {
            "name": "Amoxicillin",
            "documented_on": "2019-06-01",
            "days_since_documented": 2632,
            "dated_from": "prescription",
        }
    ]


def test_the_flag_is_never_a_hard_block():
    """A chart old enough to say so is the commonest chart this product will see.

    Blocking on it would be an alert nobody could clear, and clinicians who learn to click
    through one amber box learn to click through the red ones behind it.
    """
    flags = check_stale_medications(_ctx(ChartedMedication("Warfarin", _days_ago(900), 900)))
    assert flags[0].is_hard_block is False
    assert flags[0].severity == "warning"


def test_the_flag_says_the_drugs_were_still_checked_not_skipped():
    """The distinction from the ``unevaluated_*`` family, which the details carry explicitly.

    Those flags mean "no rule ran against this". This one means the opposite: every rule ran,
    against a list that may be out of date. A clinician who read them the same way would take a
    stale-list note as permission to ignore the flags underneath it.
    """
    flags = check_stale_medications(_ctx(ChartedMedication("Warfarin", _days_ago(900), 900)))
    assert flags[0].details["evaluated"] is True
    assert "still run against" in flags[0].summary


def test_the_boundary_is_the_curated_threshold_and_not_a_day_either_side():
    just_inside = _MEDICATION_STALE_AFTER_DAYS - 1
    assert (
        check_stale_medications(_ctx(ChartedMedication("A", _days_ago(just_inside), just_inside)))
        == []
    )
    exactly = _MEDICATION_STALE_AFTER_DAYS
    assert (
        len(check_stale_medications(_ctx(ChartedMedication("A", _days_ago(exactly), exactly)))) == 1
    )


def test_a_row_with_no_date_in_either_column_is_reported_rather_than_read_as_recent():
    """Nothing on the supported write path produces this — ``created_at`` is server-defaulted.

    It is still reported rather than skipped, because "no date at all" read as "not stale" is
    the same silence this file exists to remove, and a defensive branch that quietly returns the
    safe-looking answer is how it comes back.
    """
    flags = check_stale_medications(_ctx(ChartedMedication("Atenolol", None, None)))
    assert len(flags) == 1
    assert flags[0].details["undated_medications"] == ["Atenolol"]
    assert flags[0].details["stale_medications"] == []
    assert "no date at all" in flags[0].summary
    assert "Atenolol" in flags[0].summary


def test_a_freshly_documented_day_zero_row_is_not_mistaken_for_an_undated_one():
    """0 days is fresh, and 0 is falsy — the reason the check tests for ``None`` explicitly."""
    assert check_stale_medications(_ctx(ChartedMedication("Metformin", TODAY, 0))) == []


def test_stale_and_undated_rows_are_reported_together_in_one_flag():
    """One statement about the medication list, not one per row.

    A chart with nine aged medications must not produce nine identical amber boxes above the
    interaction the clinician needs to see.
    """
    flags = check_stale_medications(
        _ctx(
            ChartedMedication("Amlodipine", _days_ago(400), 400),
            ChartedMedication("Metformin", _days_ago(700), 700),
            ChartedMedication("Atenolol", None, None),
        )
    )
    assert len(flags) == 1
    assert [m["name"] for m in flags[0].details["stale_medications"]] == ["Metformin", "Amlodipine"]
    assert flags[0].details["undated_medications"] == ["Atenolol"]


def test_the_oldest_drug_is_the_one_named_in_the_summary():
    """Ordering is oldest-first, so the sentence quotes the row with the weakest claim to being
    current rather than whichever one the chart happened to return first."""
    flags = check_stale_medications(
        _ctx(
            ChartedMedication("Amlodipine", _days_ago(200), 200),
            ChartedMedication("Amoxicillin", _days_ago(2600), 2600),
        )
    )
    assert "“Amoxicillin”" in flags[0].summary
    assert "“Amlodipine”" not in flags[0].summary


# --- Through the service, against real rows -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_2019_prescription_is_still_evaluated_and_is_now_said_to_be_old(db):
    """Both halves at once, which is the whole design.

    The drug stays in ``current_meds`` — dropping it is how a hard block goes missing — and the
    chart-level note says the list it is in was last touched years ago.
    """
    _account, patient = await _patient(db)
    await _chart(db, patient, "Warfarin", charted_on=date(2019, 6, 1))

    service = SafetyService(db)
    ctx = await service._build_context(patient.id)
    assert [m.generic_name for m in ctx.current_meds] == ["Warfarin"]

    details = (await _notes(db, patient))["stale_medication"]
    assert [m["name"] for m in details["stale_medications"]] == ["Warfarin"]
    assert details["stale_medications"][0]["documented_on"] == "2019-06-01"


@pytest.mark.asyncio
async def test_an_old_drug_still_produces_its_interaction_flag(db):
    """The property that makes expiry the wrong fix, pinned as a hard block that must survive.

    Aspirin proposed against a warfarin last charted in 2019 is the textbook major interaction.
    If aging a medication out had been the fix, this would come back clean.
    """
    account, patient = await _patient(db)
    await _chart(db, patient, "Warfarin", charted_on=date(2019, 6, 1))

    _vocab, _ctx_, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Aspirin",
    )
    kinds = {flag.check_type for flag in flags}
    assert "drug_interaction" in kinds
    assert "stale_medication" in kinds


@pytest.mark.asyncio
async def test_a_stopped_row_is_not_counted_as_a_stale_current_one(db):
    """The list is "what the chart calls current", and a discontinued drug is not on it.

    Counting stopped rows would put every historical medication a longitudinal record exists to
    keep into a note about the current list.
    """
    _account, patient = await _patient(db)
    await _chart(db, patient, "Amoxicillin", charted_on=date(2019, 6, 1), is_current=False)

    assert "stale_medication" not in await _notes(db, patient)


@pytest.mark.asyncio
async def test_a_recently_charted_patient_gets_no_note(db):
    _account, patient = await _patient(db)
    await _chart(db, patient, "Metformin", charted_on=_days_ago(20))

    assert "stale_medication" not in await _notes(db, patient)


@pytest.mark.asyncio
async def test_a_date_the_record_could_not_have_produced_falls_back_to_the_entry_date(db):
    """An OCR'd "2126", or a prescription dated next year.

    ``_age_years`` already refuses to derive an age from an implausible date of birth. The same
    judgement applies here: a negative "days since documented" would report the least trustworthy
    row on the chart as the freshest thing on it. The row was created just now, so the fallback
    makes it fresh — correctly, and for a reason the record can defend.
    """
    _account, patient = await _patient(db)
    await _chart(db, patient, "Metformin", charted_on=date(2126, 1, 1))

    assert "stale_medication" not in await _notes(db, patient)


@pytest.mark.asyncio
async def test_an_undated_prescription_uploaded_today_is_not_called_stale(db):
    """Most of what this product ingests is handwritten, and most of that carries no readable
    date. If those rows raised a note, nearly every chart would carry one — and a warning on
    every chart is a warning nobody reads by the second week."""
    _account, patient = await _patient(db)
    await _chart(db, patient, "Metformin", charted_on=None)

    assert "stale_medication" not in await _notes(db, patient)


@pytest.mark.asyncio
async def test_an_undated_row_that_has_sat_in_the_record_for_years_is_still_caught(db):
    """The other half of the fallback: no prescription date, but the record's own entry is old.

    This is the chart nobody has opened since 2019 — the information is stale whichever column
    says so, and the note must not depend on the prescription having been legible.
    """
    _account, patient = await _patient(db)
    med = await _chart(db, patient, "Metformin", charted_on=None)
    med.created_at = datetime(2019, 6, 1, tzinfo=UTC)
    await db.flush()

    entry = (await _notes(db, patient))["stale_medication"]["stale_medications"][0]
    assert entry["name"] == "Metformin"
    assert entry["dated_from"] == "record_entry"
    assert entry["documented_on"] == "2019-06-01"


@pytest.mark.asyncio
async def test_a_brand_the_vocabulary_cannot_read_is_still_counted_in_the_list(db):
    """The staleness question is about the chart, not about what resolved.

    An unseeded brand is absent from ``current_meds`` — that is what
    ``check_unevaluated_medications`` reports — but it is a line the clinician can see under
    "Medications", so how old it is belongs in the same answer.
    """
    _account, patient = await _patient(db)
    med = await _chart(db, patient, "Metformin", charted_on=date(2019, 6, 1))
    med.generic_name = None
    med.brand_name_raw = "Zerodol-SP"
    await db.flush()

    details = (await _notes(db, patient))["stale_medication"]
    assert [m["name"] for m in details["stale_medications"]] == ["Zerodol-SP"]


@pytest.mark.asyncio
async def test_the_note_is_persisted_with_the_rest_of_the_check(db):
    """``check_type`` has a CHECK constraint behind it (migration 0023).

    A flag the column will not accept takes the whole safety check down with it, so the write
    path is exercised rather than assumed.
    """
    account, patient = await _patient(db)
    await _chart(db, patient, "Warfarin", charted_on=date(2019, 6, 1))

    _vocab, _ctx_, flags, check_ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Aspirin",
    )
    assert len(check_ids) == len(flags)
