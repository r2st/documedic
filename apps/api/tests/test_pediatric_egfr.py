"""CKD-EPI is an adult equation, and this record was applying it to children.

``_compute_derived_markers`` guarded on everything about the *creatinine* — a urine creatinine is
rejected, a µmol/L reading is rejected, a value that underflows to zero is rejected — and on
nothing at all about the patient. The only age check in ``ckd_epi_2021_egfr`` was ``age_years <=
0``, so a two-year-old's serum creatinine went into an equation fitted on adult cohorts and a
confident eGFR came back out.

It is wrong in the one direction that matters. Children have far less muscle mass than the adults
the equation was fitted on, so the same creatinine means much worse renal function in a child —
and CKD-EPI, reading that creatinine as an adult's, overestimates. The bedside Schwartz equation
(``0.413 * height_cm / Scr``) is the paediatric standard, and against it:

    2y, Scr 1.5   Schwartz 24   CKD-EPI 76
    6y, Scr 1.5   Schwartz 32   CKD-EPI 74
    15y, Scr 1.8  Schwartz 39   CKD-EPI 56

The first row is the whole problem. A two-year-old in stage 4 CKD, whose true eGFR is below the
30 mL/min/1.73m² that absolutely contraindicates metformin, had 76 written into the chart — read
as mild impairment, no threshold crossed, and the hard block never fires. This is the shape this
engine keeps being corrected for, arriving through the patient rather than through the lab: a
check that could not be run, reported as a check that passed.

Schwartz cannot simply be substituted. It needs a height and this record holds no anthropometry
at all, so the honest output is no eGFR — a state the renal checks already handle explicitly,
reporting a threshold they could not apply rather than passing it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.clinical import CKD_EPI_MIN_AGE_YEARS, ckd_epi_2021_egfr
from app.core.safety import ContraindicationRule, DrugRef, SafetyContext, evaluate_drug_safety
from app.models.derived_marker import DerivedMarker
from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.models.user import Account
from app.services.graph_service import GraphService

pytestmark = pytest.mark.asyncio


def _schwartz(height_cm: float, creatinine_mg_dl: float) -> float:
    """Bedside Schwartz, the paediatric standard — here only to state what the truth was."""
    return 0.413 * height_cm / creatinine_mg_dl


# --- the equation refuses outside the population it was fitted on -------------------------------


@pytest.mark.parametrize("age", [1, 2, 6, 10, 15, 17])
async def test_the_adult_equation_refuses_a_child(age: int) -> None:
    with pytest.raises(ValueError, match="paediatric"):
        ckd_epi_2021_egfr(creatinine_mg_dl=1.5, age_years=age, sex="male")


@pytest.mark.parametrize("age", [18, 19, 45, 90, 120])
async def test_the_adult_equation_still_answers_for_an_adult(age: int) -> None:
    """The guard must not have narrowed the population the equation is actually for."""
    assert ckd_epi_2021_egfr(creatinine_mg_dl=1.5, age_years=age, sex="male").value > 0


async def test_eighteen_is_the_boundary_and_it_is_inclusive() -> None:
    with pytest.raises(ValueError):
        ckd_epi_2021_egfr(creatinine_mg_dl=1.0, age_years=CKD_EPI_MIN_AGE_YEARS - 1, sex="female")
    assert (
        ckd_epi_2021_egfr(creatinine_mg_dl=1.0, age_years=CKD_EPI_MIN_AGE_YEARS, sex="female").value
        > 0
    )


async def test_the_refusal_is_distinguishable_from_a_nonsense_age() -> None:
    """Both raise ValueError, and the caller treats them the same — but the messages have to
    differ, because one is a data-entry error and the other is a patient this system cannot
    compute a renal function for at all."""
    with pytest.raises(ValueError, match="age must be positive"):
        ckd_epi_2021_egfr(creatinine_mg_dl=1.0, age_years=0, sex="male")


async def test_the_overestimate_this_guard_exists_for_is_real() -> None:
    """Kept as an executable statement of the harm, not just a comment: without the guard the
    equation answers, and the answer clears a threshold the child does not clear.

    Metformin's absolute contraindication is eGFR < 30. Bedside Schwartz puts this two-year-old
    at 24; the adult equation put them at 76.
    """
    truth = _schwartz(height_cm=87, creatinine_mg_dl=1.5)
    assert truth < 30  # stage 4 — metformin is absolutely contraindicated

    # What the equation returns for the same child when the guard is bypassed.
    adult_answer = ckd_epi_2021_egfr(
        creatinine_mg_dl=1.5, age_years=CKD_EPI_MIN_AGE_YEARS, sex="male"
    ).value
    assert adult_answer > 60  # comfortably clear of every renal threshold in the rule set
    assert adult_answer > truth * 2


# --- and nothing is written to the chart --------------------------------------------------------


async def _patient(db, *, born: date) -> Patient:
    account = Account(email=f"peds-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Paediatric Patient",
        sex="male",
        date_of_birth=born,
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return patient


async def _merge_creatinine(db, patient: Patient, value: str, sample: date) -> None:
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {
                "entity_type": "lab_result",
                "fields": {
                    "marker_name": "Serum Creatinine",
                    "value_numeric": value,
                    "unit": "mg/dL",
                    "sample_date": sample.isoformat(),
                },
            }
        ],
    )
    await db.flush()


async def _derived_egfrs(db, patient: Patient) -> list[DerivedMarker]:
    rows = await db.execute(
        select(DerivedMarker).where(
            DerivedMarker.patient_id == patient.id, DerivedMarker.marker_name == "eGFR"
        )
    )
    return list(rows.scalars().all())


async def test_no_egfr_is_derived_for_a_child(db) -> None:
    """The number that would have been written is 76 on a child whose kidneys are at 24."""
    patient = await _patient(db, born=date(2024, 3, 1))

    await _merge_creatinine(db, patient, "1.5", date(2026, 6, 1))

    assert await _derived_egfrs(db, patient) == []


async def test_the_lab_result_itself_is_still_recorded(db) -> None:
    """Refusing to derive must not cost the clinician the creatinine. The measurement is real
    and belongs in the chart; only the adult equation applied to it does not."""
    patient = await _patient(db, born=date(2024, 3, 1))

    await _merge_creatinine(db, patient, "1.5", date(2026, 6, 1))

    labs = (
        (await db.execute(select(LabResult).where(LabResult.patient_id == patient.id)))
        .scalars()
        .all()
    )
    assert [lab.value_numeric for lab in labs] == [Decimal("1.5")]


async def test_an_adult_on_the_same_chart_shape_still_gets_one(db) -> None:
    """The guard is on the patient, not on the pipeline."""
    patient = await _patient(db, born=date(1980, 3, 1))

    await _merge_creatinine(db, patient, "1.5", date(2026, 6, 1))

    assert len(await _derived_egfrs(db, patient)) == 1


async def test_a_patient_who_turns_eighteen_between_two_draws_is_computed_from_the_later_one(
    db,
) -> None:
    """Age is taken at the sample date, not at today's date — this product reads histories, and
    a childhood creatinine must not acquire an eGFR just because the patient has since grown up.
    The converse is what this asserts: the adult draw is computed normally."""
    patient = await _patient(db, born=date(2008, 6, 1))

    await _merge_creatinine(db, patient, "1.5", date(2025, 1, 1))  # aged 16
    await _merge_creatinine(db, patient, "1.5", date(2026, 8, 1))  # aged 18

    derived = await _derived_egfrs(db, patient)
    assert len(derived) == 1
    assert derived[0].input_values["age_years"] == 18


# --- what the clinician sees instead ------------------------------------------------------------


async def test_the_renal_rule_reports_itself_unevaluated_rather_than_passing() -> None:
    """The payoff, and the reason refusing is safe. With no eGFR on the chart the renal branch
    already says the threshold could not be applied — so a metformin check on a child now warns
    that the rule did not run, where before it silently passed on a fabricated 76."""
    metformin = DrugRef(reference_id="MET-500", generic_name="Metformin", drug_class="biguanide")
    rule = ContraindicationRule(
        "MET-500",
        "Chronic Kidney Disease",
        "dose_adjustment_required",
        "Metformin is contraindicated below eGFR 30 (lactic acidosis)",
        False,
        renal_threshold={"egfr_below": 30, "action": "contraindicated"},
    )

    flags = evaluate_drug_safety(metformin, SafetyContext(contraindication_rules=[rule], egfr=None))

    renal = [f for f in flags if f.check_type == "renal_dose"]
    assert len(renal) == 1
    assert renal[0].details["evaluated"] is False
    assert renal[0].is_hard_block is False
