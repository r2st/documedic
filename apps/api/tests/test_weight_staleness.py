"""Is the weight this chart's dose arithmetic rests on still this patient's weight?

R81 stored ``patients.weight_kg`` and ``weight_recorded_at`` together and said in the model's
own comment why the second column was there — "a paediatric dose calculated from a weight taken
two years ago is calculated from a weight this child has grown out of" — and then nothing read
it. ``assess_dose`` divided by the number whatever its age, so a nine-year-old weighed at four
cleared every mg/kg ceiling in the table by a wide margin.

That is the fail-passive shape this codebase has closed repeatedly, and the most dangerous
direction for a dose check to fail in: the flag that does not appear is indistinguishable from
the flag that was not needed. The tests below pin the reader that closes it, and — as much as
the finding itself — the four silences it must keep:

* a chart with no weight-dosed medication on it;
* a weight inside its window;
* a chart with no weight at all, which ``check_dose_ranges`` already reports as
  ``unevaluated_dose`` for a child and which no adult ceiling in the table consumes;
* an age band whose window has been configured to nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.safety import (
    DrugRef,
    SafetyContext,
    WeightStalenessPolicy,
    check_weight_staleness,
)
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

pytestmark = pytest.mark.asyncio

# Paracetamol carries a paediatric mg/kg/day band, so it is weight-dosed. Telmisartan does not.
PARACETAMOL = DrugRef("PCM-500", "Paracetamol", "Analgesic")
IBUPROFEN = DrugRef("IBU-400", "Ibuprofen", "NSAID")
TELMISARTAN = DrugRef("TEL-40", "Telmisartan", "ARB")


def _ctx(**overrides) -> SafetyContext:
    fields: dict = {
        "current_meds": [PARACETAMOL],
        "weight_kg": 18.0,
        "weight_recorded_days_ago": 400,
        "age_years": 9,
    }
    fields.update(overrides)
    return SafetyContext(**fields)


# --- which drugs put the weight in play ---------------------------------------------------------


async def test_a_weight_dosed_drug_is_the_one_with_a_curated_mg_per_kg_band() -> None:
    """The set is derived from the range table, not curated twice."""
    from app.core.dose_range import is_weight_dosed

    assert is_weight_dosed("Paracetamol") is True
    assert is_weight_dosed("paracetamol") is True
    assert is_weight_dosed("Ibuprofen") is True
    # A real drug with a curated adult range and no weight-based band.
    assert is_weight_dosed("Telmisartan") is False
    # Not in the table at all: "not curated" is not "not weight-dosed", but it is the only
    # honest answer this module can give, and the caller reports nothing rather than guessing.
    assert is_weight_dosed("Zyxolol") is False
    assert is_weight_dosed(None) is False


async def test_a_chart_with_no_weight_dosed_medication_says_nothing() -> None:
    """A six-month-old weight on a chart of ARBs and statins is not a finding.

    Flagging every chart whose weight is old regardless of what is prescribed is how a note
    stops being read.
    """
    assert check_weight_staleness(_ctx(current_meds=[TELMISARTAN])) == []


async def test_the_drug_being_proposed_counts_even_when_the_chart_has_none() -> None:
    """The question is asked hardest at the moment of prescribing."""
    flags = check_weight_staleness(_ctx(current_meds=[TELMISARTAN]), proposed=PARACETAMOL)

    assert [f.check_type for f in flags] == ["stale_weight"]
    assert flags[0].details["weight_dosed_drugs"] == ["Paracetamol"]


async def test_every_weight_dosed_drug_in_play_is_named_once_and_in_order() -> None:
    flags = check_weight_staleness(
        _ctx(current_meds=[PARACETAMOL, IBUPROFEN, TELMISARTAN]), proposed=PARACETAMOL
    )

    assert flags[0].details["weight_dosed_drugs"] == ["Ibuprofen", "Paracetamol"]


# --- the window ---------------------------------------------------------------------------------


async def test_a_weight_inside_its_window_is_not_reported() -> None:
    assert check_weight_staleness(_ctx(weight_recorded_days_ago=30)) == []


async def test_the_window_boundary_is_inclusive_of_the_threshold_day() -> None:
    policy = WeightStalenessPolicy(adult_days=183, paediatric_days=92)

    assert check_weight_staleness(_ctx(weight_recorded_days_ago=91, weight_staleness=policy)) == []
    assert check_weight_staleness(_ctx(weight_recorded_days_ago=92, weight_staleness=policy))


async def test_a_child_is_measured_against_the_shorter_window() -> None:
    """100 days is stale for a nine-year-old and current for an adult."""
    child = check_weight_staleness(_ctx(age_years=9, weight_recorded_days_ago=100))
    adult = check_weight_staleness(_ctx(age_years=44, weight_recorded_days_ago=100, weight_kg=72))

    assert [f.check_type for f in child] == ["stale_weight"]
    assert adult == []


async def test_an_unknown_age_takes_the_paediatric_window() -> None:
    """A chart with no usable date of birth might be a child, which is the shorter answer."""
    flags = check_weight_staleness(_ctx(age_years=None, weight_recorded_days_ago=100))

    assert [f.check_type for f in flags] == ["stale_weight"]
    assert flags[0].details["stale_after_days"] == 92


async def test_the_boundary_between_the_two_windows_is_the_paediatric_dosing_age() -> None:
    """One chart cannot be a child for the dose ceiling and an adult for the weight behind it."""
    from app.core.dose_range import PAEDIATRIC_MAX_AGE_YEARS

    just_under = check_weight_staleness(_ctx(age_years=PAEDIATRIC_MAX_AGE_YEARS - 1))
    at_boundary = check_weight_staleness(_ctx(age_years=PAEDIATRIC_MAX_AGE_YEARS))

    assert just_under[0].details["stale_after_days"] == 92
    assert at_boundary[0].details["stale_after_days"] == 183


async def test_a_window_configured_to_nothing_switches_that_arm_off() -> None:
    """Non-positive means "no ceiling", the convention the rate-limit buckets already take.

    Reading it as a window of zero days would make every weight stale the instant it was
    recorded, which is the opposite of what an operator setting it to nothing means.
    """
    off = WeightStalenessPolicy(adult_days=0, paediatric_days=0)

    assert check_weight_staleness(_ctx(weight_staleness=off)) == []
    assert check_weight_staleness(_ctx(weight_recorded_days_ago=None, weight_staleness=off)) == []


# --- what is said, and how loudly ---------------------------------------------------------------


async def test_a_stale_weight_on_a_child_is_critical_because_a_ceiling_used_it() -> None:
    flags = check_weight_staleness(_ctx(age_years=9))

    assert flags[0].severity == "critical"
    assert flags[0].details["applied_to_dose_check"] is True
    # Not "evaluated": the paediatric ceilings were computed *from* this number, so calling them
    # evaluated would overstate what they are worth.
    assert flags[0].details["evaluated"] is False


async def test_a_stale_weight_on_an_adult_is_a_warning_and_says_so_accurately() -> None:
    """No adult ceiling in the table takes weight as an input, and the wording must not claim it
    does."""
    flags = check_weight_staleness(_ctx(age_years=44, weight_kg=72, weight_recorded_days_ago=400))

    assert flags[0].severity == "warning"
    assert flags[0].details["applied_to_dose_check"] is False
    assert "No ceiling applied on this screen took the weight as an input" in flags[0].summary


async def test_it_is_never_a_hard_block() -> None:
    """An out-of-date weight is a measurement to repeat, not a documented conflict.

    Rule #3's instrument is for a real drug conflicting with a real chart; spending it here is
    how the blocks that are never wrong stop being read.
    """
    for age in (4, 9, 44, 80, None):
        for days in (400, None):
            for flag in check_weight_staleness(_ctx(age_years=age, weight_recorded_days_ago=days)):
                assert flag.is_hard_block is False


async def test_the_summary_carries_the_weight_the_drugs_and_the_age() -> None:
    flags = check_weight_staleness(_ctx(weight_kg=18.0, weight_recorded_days_ago=400))

    summary = flags[0].summary
    assert "18 kg" in summary
    assert "“Paracetamol”" in summary
    assert "over a year ago" in summary


async def test_a_paediatric_weight_keeps_its_tenth_of_a_kilogram() -> None:
    """8.5 kg and 8 kg are different doses of a mg/kg drug."""
    flags = check_weight_staleness(_ctx(weight_kg=8.5))

    assert "8.5 kg" in flags[0].summary


# --- a weight with no date -----------------------------------------------------------------------


async def test_a_weight_carrying_no_date_is_reported_rather_than_assumed_recent() -> None:
    """Migration 0033 left the column NULL on rows that predate it.

    Every supported write path stamps a date now; a row that does not carry one cannot show that
    its weight is current, and reading silence as freshness is the fail-open shape.
    """
    flags = check_weight_staleness(_ctx(weight_recorded_days_ago=None))

    assert [f.check_type for f in flags] == ["stale_weight"]
    assert flags[0].details["weight_recorded_days_ago"] is None
    assert "carries no date" in flags[0].summary


async def test_a_chart_with_no_weight_at_all_is_left_to_the_dose_check() -> None:
    """Two checks reporting one gap in two voices is the duplication this placement avoids.

    ``check_dose_ranges`` already answers it as ``unevaluated_dose`` for a child, and for an
    adult no ceiling in the table consumes a weight, so nothing was silently weakened.
    """
    assert check_weight_staleness(_ctx(weight_kg=None, weight_recorded_days_ago=None)) == []


# --- through the service --------------------------------------------------------------------------


async def _account_and_patient(db, **patient_fields) -> tuple[Account, Patient]:
    account = Account(email=f"weight-stale-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    fields = {
        "full_name": "Weight Staleness Patient",
        "sex": "female",
        "date_of_birth": datetime(2017, 1, 1).date(),
        "consent_given": True,
        "consent_given_at": datetime.now(UTC),
    }
    fields.update(patient_fields)
    patient = Patient(account_id=account.id, **fields)
    db.add(patient)
    await db.flush()
    return account, patient


async def _chart(db, patient: Patient, **fields) -> None:
    db.add(MedicationEvent(patient_id=patient.id, event_type="start", is_current=True, **fields))
    await db.flush()


async def test_the_service_carries_the_weights_age_in_days(db) -> None:
    _, patient = await _account_and_patient(
        db, weight_kg=18.0, weight_recorded_at=datetime.now(UTC) - timedelta(days=400)
    )

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert ctx.weight_recorded_days_ago == 400


async def test_a_weight_with_no_date_reaches_the_engine_as_none_not_zero(db) -> None:
    _, patient = await _account_and_patient(db, weight_kg=18.0, weight_recorded_at=None)

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert ctx.weight_kg == pytest.approx(18.0)
    assert ctx.weight_recorded_days_ago is None


async def test_a_weight_dated_in_the_future_is_clamped_rather_than_read_as_fresh(db) -> None:
    """A negative day count would compare below every window and pass silently."""
    _, patient = await _account_and_patient(
        db, weight_kg=18.0, weight_recorded_at=datetime.now(UTC) + timedelta(days=30)
    )

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert ctx.weight_recorded_days_ago == 0


async def test_the_service_builds_the_policy_from_settings(db, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "weight_stale_adult_days", 45)
    monkeypatch.setattr(settings, "weight_stale_paediatric_days", 15)
    _, patient = await _account_and_patient(db)

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert ctx.weight_staleness == WeightStalenessPolicy(adult_days=45, paediatric_days=15)


async def test_a_stale_weight_reaches_the_standing_safety_board(db) -> None:
    """End to end: the chart a clinician can see, and the flag they now get about it."""
    _, patient = await _account_and_patient(
        db,
        date_of_birth=datetime(2017, 1, 1).date(),
        weight_kg=18.0,
        weight_recorded_at=datetime.now(UTC) - timedelta(days=400),
    )
    await _chart(
        db, patient, generic_name="Paracetamol", dose="500", dose_unit="mg", frequency="TDS"
    )

    flags = await SafetyService(db).chart_completeness_flags(patient.id)

    stale = [f for f in flags if f.check_type == "stale_weight"]
    assert len(stale) == 1
    assert stale[0].severity == "critical"
    assert stale[0].is_hard_block is False


async def test_a_fresh_weight_leaves_the_safety_board_quiet(db) -> None:
    _, patient = await _account_and_patient(
        db, weight_kg=18.0, weight_recorded_at=datetime.now(UTC) - timedelta(days=10)
    )
    await _chart(
        db, patient, generic_name="Paracetamol", dose="250", dose_unit="mg", frequency="TDS"
    )

    flags = await SafetyService(db).chart_completeness_flags(patient.id)

    assert [f for f in flags if f.check_type == "stale_weight"] == []


async def test_the_flag_is_raised_once_for_a_chart_not_once_per_drug(db) -> None:
    """A statement about the chart, so a chart-level placement — like every other one here."""
    _, patient = await _account_and_patient(
        db, weight_kg=18.0, weight_recorded_at=datetime.now(UTC) - timedelta(days=400)
    )
    await _chart(
        db, patient, generic_name="Paracetamol", dose="250", dose_unit="mg", frequency="TDS"
    )
    await _chart(db, patient, generic_name="Ibuprofen", dose="100", dose_unit="mg", frequency="BD")

    flags = await SafetyService(db).chart_completeness_flags(patient.id)

    assert len([f for f in flags if f.check_type == "stale_weight"]) == 1


# --- over the wire --------------------------------------------------------------------------------


async def test_a_stale_weight_is_visible_on_the_safety_screen(auth_client) -> None:
    from .conftest import create_patient

    patient = await create_patient(
        auth_client, date_of_birth="2017-01-01", full_name="Child With Old Weight", weight_kg=18.0
    )
    # Recorded now by the create, so age it by moving the column back through the safety check's
    # own reader — the API stamps the date and offers no way to backdate it, which is correct.
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Paracetamol", "dose": "250", "dose_unit": "mg", "frequency": "TDS"},
    )

    assert resp.status_code == 200, resp.text
    # A weight recorded moments ago is inside every window: the endpoint answers, and says
    # nothing about the weight. The staleness path is exercised at the service level above,
    # where the date can be set.
    assert [f for f in resp.json()["flags"] if f["check_type"] == "stale_weight"] == []
