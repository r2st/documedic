"""Nothing in the deterministic engine could see how old the patient was.

An interaction rule is a pair of drugs. A contraindication rule is a drug and a charted
condition. Being 82 is neither — so the whole published body of age-based prescribing criteria
had no shape in this engine to be expressed as, and the only age this codebase had ever read was
CKD-EPI's paediatric floor (R47).

That is not an abstract gap. Four of the fifty seeded drugs are named directly by those criteria:

  * glimepiride — a sulfonylurea, prolonged hypoglycaemia in older adults, which presents as a
    fall or as confusion rather than as a recognised hypo;
  * diclofenac and ibuprofen — NSAIDs, GI bleeding and acute kidney injury, risk rising sharply
    with age and further alongside the antithrombotics ``check_bleeding_burden`` now counts;
  * digoxin — seeded at 0.25 mg, which is exactly the strength the criteria caution against
    exceeding 0.125 mg/day of in older adults;
  * omeprazole and pantoprazole — PPIs, scheduled use beyond about eight weeks.

An 88-year-old on glimepiride and digoxin produced no age-related finding at all.

Two things this file pins as hard as the findings themselves. The cautions are *cautions* —
never a block, never critical — because every one of these drugs is correctly prescribed to
older patients daily. And an unknown date of birth is reported as a check that did not run
rather than one that passed, which is the contract the three ``unevaluated_*`` types already
carry and the failure mode this engine has been corrected for repeatedly.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.safety import (
    _GERIATRIC_AGE_YEARS,
    DrugRef,
    SafetyContext,
    check_geriatric_cautions,
    evaluate_drug_safety,
)
from app.models.patient import Patient
from app.services.safety_service import SafetyService
from tests.test_safety_active_flags import _account_and_patient, _add_current_med, _vocab

_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "drugs"
VOCABULARY = json.loads((_DATA_DIR / "drug_vocabulary.json").read_text())
_BY_REFERENCE_ID = {row["reference_id"]: row for row in VOCABULARY}

GLIMEPIRIDE = DrugRef(reference_id="GLM-1", generic_name="Glimepiride", drug_class="Sulfonylurea")
DIGOXIN = DrugRef(reference_id="DIG-0.25", generic_name="Digoxin", drug_class="Cardiac glycoside")
DICLOFENAC = DrugRef(reference_id="DIC-50", generic_name="Diclofenac", drug_class="NSAID")
PANTOPRAZOLE = DrugRef(reference_id="PAN-40", generic_name="Pantoprazole", drug_class="PPI")
AMLODIPINE = DrugRef(reference_id="AML-5", generic_name="Amlodipine", drug_class="CCB")


def _flags(drug: DrugRef, age: int | None):
    return check_geriatric_cautions(drug, SafetyContext(age_years=age))


# --- Whether it fires -----------------------------------------------------------------------------


@pytest.mark.parametrize("drug", [GLIMEPIRIDE, DIGOXIN, DICLOFENAC, PANTOPRAZOLE])
def test_each_curated_class_is_flagged_for_a_patient_over_the_threshold(drug):
    flags = _flags(drug, 82)
    assert len(flags) == 1
    assert flags[0].check_type == "geriatric_caution"
    assert flags[0].details["evaluated"] is True
    assert flags[0].details["age_years"] == 82


@pytest.mark.parametrize("drug", [GLIMEPIRIDE, DIGOXIN, DICLOFENAC, PANTOPRAZOLE])
def test_the_same_drug_is_not_flagged_for_a_younger_patient(drug):
    """The criteria are about older adults. Firing on a 40-year-old would make the badge
    meaningless on the chart it was written for."""
    assert _flags(drug, 40) == []


def test_a_drug_no_criterion_names_is_never_flagged_however_old_the_patient():
    """Amlodipine is prescribed to almost every elderly hypertensive in this market. A check
    that fired on age alone would put a caution under every drug on every geriatric chart."""
    assert _flags(AMLODIPINE, 95) == []


def test_the_boundary_year_is_included():
    """ "65 or over", not "over 65" — an off-by-one here silently drops a whole birth year."""
    assert _flags(GLIMEPIRIDE, _GERIATRIC_AGE_YEARS) != []
    assert _flags(GLIMEPIRIDE, _GERIATRIC_AGE_YEARS - 1) == []


# --- An age the record cannot supply --------------------------------------------------------------


def test_an_unknown_date_of_birth_says_the_check_did_not_run():
    """The failure this engine keeps being corrected for: a comparison that could not be
    attempted, reported as a comparison that passed."""
    flags = _flags(GLIMEPIRIDE, None)
    assert len(flags) == 1
    assert flags[0].severity == "info"
    assert flags[0].details["evaluated"] is False
    assert flags[0].details["reason"] == "no_date_of_birth"
    assert "not evaluated" in flags[0].summary
    assert "did not run, not a check that passed" in flags[0].summary


def test_an_unknown_age_raises_nothing_for_a_drug_that_carries_no_caution():
    """Scoped to drugs that actually have something to say, so a record with no date of birth
    does not grow an informational notice underneath every medication on it."""
    assert _flags(AMLODIPINE, None) == []


# --- How much weight it carries -------------------------------------------------------------------


@pytest.mark.parametrize("drug", [GLIMEPIRIDE, DIGOXIN, DICLOFENAC, PANTOPRAZOLE])
@pytest.mark.parametrize("age", [None, 82])
def test_an_age_based_caution_is_never_a_block_and_never_critical(drug, age):
    """Every one of these drugs is correctly prescribed to older patients daily. The criteria
    say the decision deserves a second look, and a second look is a sentence on the screen."""
    for flag in check_geriatric_cautions(drug, SafetyContext(age_years=age)):
        assert flag.is_hard_block is False
        assert flag.severity in ("info", "warning")


# --- What it says ---------------------------------------------------------------------------------


def test_the_wording_is_prescriber_framed_and_claims_no_certainty():
    """Critical Safety Rule #4. "Stop the glimepiride" is a bug; "guidelines support
    considering" is the form."""
    flag = _flags(GLIMEPIRIDE, 82)[0]
    assert "Guidelines support considering" in flag.summary
    for imperative in ("Stop ", "Give ", "Administer ", "Switch the patient", "The patient has "):
        assert imperative not in flag.summary


def test_the_finding_names_the_criterion_it_came_from():
    """Ungrounded, this is the system telling a clinician their prescription is questionable on
    no stated authority. Every entry cites the criteria it is taken from."""
    flag = _flags(DIGOXIN, 82)[0]
    assert "Beers" in flag.summary
    assert flag.details["reference"] == flag.details["reference"].strip()
    assert "Beers" in flag.details["reference"]


def test_the_digoxin_caution_admits_it_could_not_read_the_daily_dose():
    """The seeded row is a 0.25 mg tablet and the threshold is 0.125 mg/day — but half a tablet
    daily is 0.125, and this record carries the dose as free text ("1 tab OD"). Asserting the
    patient is over the threshold from the tablet strength would be exactly the confident-wrong
    number the eGFR work refused twice."""
    flag = _flags(DIGOXIN, 82)[0]
    assert "0.125" in flag.summary
    assert "no structured daily dose" in flag.summary


def test_the_ppi_caution_admits_it_could_not_read_the_duration():
    """Same shape: the criterion is about scheduled use beyond eight weeks, and no structured
    treatment duration reaches this engine."""
    flag = _flags(PANTOPRAZOLE, 82)[0]
    assert "no structured treatment duration" in flag.summary


def test_the_nsaid_caution_names_the_combination_that_makes_it_worse():
    """The tie to ``check_bleeding_burden``: an NSAID in an older adult is a caution, and an
    NSAID in an older adult already on an anticoagulant is the admission."""
    flag = _flags(DICLOFENAC, 82)[0]
    assert "anticoagulant" in flag.summary


# --- Fixed-dose combinations ----------------------------------------------------------------------


def test_a_combination_carries_its_components_cautions():
    """Glycomet GP is among the most prescribed diabetes products in this market. Its product
    class is "Biguanide + Sulfonylurea", which is not "Sulfonylurea" and matches nothing — and
    its glimepiride component is the entire reason the criteria name it."""
    row = _BY_REFERENCE_ID["MET-GLM-1-500"]
    combo = DrugRef(
        reference_id=row["reference_id"],
        generic_name=row["generic_name"],
        drug_class=row["drug_class"],
        components=tuple(
            DrugRef(
                reference_id=c.get("reference_id", ""),
                generic_name=c["generic_name"],
                drug_class=c.get("drug_class"),
            )
            for c in row["components"]
        ),
    )
    flags = check_geriatric_cautions(combo, SafetyContext(age_years=82))

    assert len(flags) == 1
    # Named as the product the clinician is prescribing, with the component that matched.
    assert flags[0].details["proposed_drug"] == row["generic_name"]
    assert flags[0].details["component"] == "Glimepiride"


def test_one_caution_per_criterion_even_if_two_identities_match_it():
    """A combination of two NSAIDs would otherwise raise the same criterion twice under one
    prescription, which reads as two separate findings about one drug."""
    combo = DrugRef(
        reference_id="NSAID-COMBO",
        generic_name="Diclofenac + Ibuprofen",
        drug_class="NSAID",
        components=(
            DrugRef(reference_id="DIC-50", generic_name="Diclofenac", drug_class="NSAID"),
            DrugRef(reference_id="IBU-400", generic_name="Ibuprofen", drug_class="NSAID"),
        ),
    )
    assert len(check_geriatric_cautions(combo, SafetyContext(age_years=82))) == 1


# --- Wiring, and the age the service actually derives -------------------------------------------


def test_the_check_runs_as_part_of_the_standard_evaluation():
    flags = evaluate_drug_safety(GLIMEPIRIDE, SafetyContext(age_years=82))
    assert any(f.check_type == "geriatric_caution" for f in flags)


def test_the_check_type_is_one_the_audit_table_will_accept():
    from app.models.drug_safety_check import DrugSafetyCheck

    constraint = next(c for c in DrugSafetyCheck.__table_args__ if c.name == "ck_dsc_check_type")
    assert "geriatric_caution" in str(constraint.sqltext)


async def _patient_born(db, born: date | None):
    account, patient = await _account_and_patient(db)
    patient.date_of_birth = born
    await db.flush()
    return account, patient


@pytest.mark.asyncio
async def test_the_service_derives_the_age_and_the_flag_reaches_the_chart(db) -> None:
    account, patient = await _patient_born(db, date(datetime.now(UTC).year - 82, 1, 1))
    if await _vocab(db, "Glimepiride") is None:
        pytest.skip("seed corpus lacks Glimepiride")
    await _add_current_med(db, patient, "Glimepiride")

    results = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    flags = [f for _vocab_row, group in results for f in group]

    geriatric = [f for f in flags if f.check_type == "geriatric_caution"]
    assert len(geriatric) == 1
    assert geriatric[0].details["evaluated"] is True
    assert geriatric[0].details["age_years"] in (81, 82)


@pytest.mark.asyncio
async def test_a_record_with_no_date_of_birth_reaches_the_chart_as_unevaluated(db) -> None:
    account, patient = await _patient_born(db, None)
    if await _vocab(db, "Glimepiride") is None:
        pytest.skip("seed corpus lacks Glimepiride")
    await _add_current_med(db, patient, "Glimepiride")

    results = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    geriatric = [f for _v, group in results for f in group if f.check_type == "geriatric_caution"]

    assert len(geriatric) == 1
    assert geriatric[0].details["evaluated"] is False


@pytest.mark.asyncio
async def test_an_implausible_date_of_birth_is_not_turned_into_an_age(db) -> None:
    """A DOB read off a scan as a future year is the OCR failure ``is_plausible_clinical_date``
    exists for. Deriving an age from it would produce a negative number, and the branch that
    number lands in decides whether an elderly patient's caution is shown at all."""
    account, patient = await _patient_born(db, date(datetime.now(UTC).year + 40, 6, 1))
    if await _vocab(db, "Glimepiride") is None:
        pytest.skip("seed corpus lacks Glimepiride")
    await _add_current_med(db, patient, "Glimepiride")

    ctx = await SafetyService(db)._patient_facts(patient.id)
    assert ctx.age_years is None

    results = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    geriatric = [f for _v, group in results for f in group if f.check_type == "geriatric_caution"]
    assert [f.details["evaluated"] for f in geriatric] == [False]


@pytest.mark.asyncio
async def test_the_patients_own_date_of_birth_is_the_one_used(db) -> None:
    """Two patients on one account, one elderly and one not. The context is memoised per
    patient, and a leak across that cache would put an age-based caution on the wrong chart."""
    account, elderly = await _patient_born(db, date(datetime.now(UTC).year - 80, 1, 1))
    if await _vocab(db, "Glimepiride") is None:
        pytest.skip("seed corpus lacks Glimepiride")
    await _add_current_med(db, elderly, "Glimepiride")

    younger = Patient(
        account_id=account.id,
        full_name="Younger Patient",
        sex="female",
        date_of_birth=date(datetime.now(UTC).year - 30, 1, 1),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(younger)
    await db.flush()
    await _add_current_med(db, younger, "Glimepiride")

    service = SafetyService(db)
    old_flags = await service.active_flags(account_id=account.id, patient_id=elderly.id)
    young_flags = await service.active_flags(account_id=account.id, patient_id=younger.id)

    def geriatric(results):
        return [f for _v, g in results for f in g if f.check_type == "geriatric_caution"]

    assert len(geriatric(old_flags)) == 1
    assert geriatric(young_flags) == []

    # And the row is really there — the younger patient's silence is about her age, not about a
    # missing medication.
    assert await db.scalar(select(Patient.id).where(Patient.id == younger.id)) == younger.id
