"""The age axis only ever pointed one way.

``check_geriatric_cautions`` was added because nothing in the deterministic engine could see that
a patient was 82 — an interaction rule is a pair of drugs and a contraindication rule is a drug
and a charted condition, and being 82 is neither. Nothing could see that a patient was four
either, and that is the end of the axis where the drugs in question are worse than merely
inadvisable:

  * aspirin, seeded twice (ASP-75, ASP-150), is associated with Reye's syndrome in children — an
    encephalopathy with hepatic failure, and one that follows exactly the viral illness a child
    is brought in with. "Disprin for the fever" is an over-the-counter decision in this market,
    so the chart being read may already carry it before anyone prescribed anything;
  * ciprofloxacin (CIP-500) is a fluoroquinolone, restricted in growing patients by the published
    safety reviews to infections where no other agent is suitable.

A four-year-old on aspirin produced no age-related finding at all.

What this file pins beyond the findings themselves. The severities are graded *per entry* rather
than fixed the way the geriatric ones are, because the two ends of the axis ask different
questions — an older-adult criterion says a routine drug deserves a second look, a paediatric one
says this is the wrong drug for this age. Neither end is ever a hard block, for the reason Rule
#3 exists: both entries have a correct paediatric use (aspirin in Kawasaki disease, a
fluoroquinolone in a resistant infection), so a block would be overridden routinely, which is how
a block stops being read. And an unknown date of birth is reported as a check that did not run
rather than one that passed — which matters more here than at the other end, because a child's
paper record in this market frequently carries a hand-written age rather than a date.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.core.safety import (
    DrugRef,
    SafetyContext,
    check_paediatric_cautions,
    evaluate_drug_safety,
)
from app.services.safety_service import SafetyService
from tests.test_safety_active_flags import _account_and_patient, _add_current_med, _vocab

ASPIRIN = DrugRef(reference_id="ASP-75", generic_name="Aspirin", drug_class="Antiplatelet")
CIPROFLOXACIN = DrugRef(
    reference_id="CIP-500", generic_name="Ciprofloxacin", drug_class="Fluoroquinolone"
)
CLOPIDOGREL = DrugRef(reference_id="CLO-75", generic_name="Clopidogrel", drug_class="Antiplatelet")
PARACETAMOL = DrugRef(reference_id="PCM-500", generic_name="Paracetamol", drug_class="Analgesic")


def _flags(drug: DrugRef, age: int | None):
    return check_paediatric_cautions(drug, SafetyContext(age_years=age))


# --- Whether it fires -----------------------------------------------------------------------------


@pytest.mark.parametrize("drug", [ASPIRIN, CIPROFLOXACIN])
def test_each_curated_entry_is_flagged_for_a_child(drug):
    flags = _flags(drug, 6)
    assert len(flags) == 1
    assert flags[0].check_type == "paediatric_caution"
    assert flags[0].details["evaluated"] is True
    assert flags[0].details["age_years"] == 6


@pytest.mark.parametrize("drug", [ASPIRIN, CIPROFLOXACIN])
def test_the_same_drug_is_not_flagged_for_an_adult(drug):
    """Aspirin in a 55-year-old is the single most ordinary line on a cardiology chart. A caution
    that fired there would be under half the prescriptions in the system."""
    assert _flags(drug, 55) == []


def test_a_drug_no_caution_names_is_never_flagged_however_young_the_patient():
    """Paracetamol is the drug the aspirin caution points *to*. Firing on age alone would put a
    notice under every medication on every paediatric chart, including the recommended one."""
    assert _flags(PARACETAMOL, 3) == []


def test_the_caution_belongs_to_the_molecule_not_to_its_class():
    """Aspirin's seeded class is "Antiplatelet", and so is clopidogrel's. Reye's syndrome is a
    fact about salicylates, not about platelet inhibition — keying this on the class would file a
    warning about a childhood encephalopathy under a drug that has nothing to do with one."""
    assert _flags(CLOPIDOGREL, 6) == []
    assert _flags(CLOPIDOGREL, None) == []


def test_the_caution_can_also_belong_to_a_class():
    """The other key. The fluoroquinolone restriction is about the family, so a
    ``generic_name`` this table has never heard of still matches on its class."""
    levofloxacin = DrugRef(
        reference_id="LEV-500", generic_name="Levofloxacin", drug_class="Fluoroquinolone"
    )
    assert len(_flags(levofloxacin, 8)) == 1


@pytest.mark.parametrize(
    "drug,threshold",
    [(ASPIRIN, 16), (CIPROFLOXACIN, 18)],
)
def test_the_boundary_year_is_excluded(drug, threshold):
    """ "Under 16", not "16 or under" — and unlike the geriatric threshold this comparison is
    strict, so an off-by-one here does not merely shift a birth year, it disagrees with the
    printed criterion in the direction of flagging adults."""
    assert _flags(drug, threshold - 1) != []
    assert _flags(drug, threshold) == []


def test_the_two_entries_carry_their_own_thresholds():
    """A shared constant would be wrong in both directions: it would either stop cautioning
    17-year-olds about fluoroquinolones or start cautioning them about aspirin."""
    assert _flags(ASPIRIN, 17) == []
    assert _flags(CIPROFLOXACIN, 17) != []


# --- An age the record cannot supply --------------------------------------------------------------


def test_an_unknown_date_of_birth_says_the_check_did_not_run():
    """This engine's recurring failure, and the one this branch exists to refuse: a comparison
    that could not be attempted, reported as a comparison that passed."""
    flags = _flags(ASPIRIN, None)
    assert len(flags) == 1
    assert flags[0].severity == "info"
    assert flags[0].details["evaluated"] is False
    assert flags[0].details["reason"] == "no_date_of_birth"
    assert "not evaluated" in flags[0].summary
    assert "did not run, not a check that passed" in flags[0].summary


def test_an_unknown_age_raises_nothing_for_a_drug_that_carries_no_caution():
    """Scoped to drugs with something to say, so a record with no date of birth does not grow an
    informational notice underneath every medication on it."""
    assert _flags(PARACETAMOL, None) == []


# --- How much weight it carries -------------------------------------------------------------------


@pytest.mark.parametrize("drug", [ASPIRIN, CIPROFLOXACIN])
@pytest.mark.parametrize("age", [None, 6])
def test_a_paediatric_caution_is_never_a_hard_block(drug, age):
    """Rule #3's hard block is for a documented conflict between a real drug and a real chart,
    and it demands an override with written reasoning. Both entries here have a correct
    paediatric use, so a block would be overridden routinely — which is how a block stops being
    read, including on the charts where it was right."""
    for flag in check_paediatric_cautions(drug, SafetyContext(age_years=age)):
        assert flag.is_hard_block is False


def test_the_severities_are_graded_per_entry_rather_than_fixed():
    """The geriatric table is uniformly "warning" because its entries all say the same kind of
    thing. These two do not: one is a named, potentially fatal association with the patient's
    age, and the other is a restriction with a documented exception."""
    assert _flags(ASPIRIN, 6)[0].severity == "critical"
    assert _flags(CIPROFLOXACIN, 6)[0].severity == "warning"


# --- What it says ---------------------------------------------------------------------------------


@pytest.mark.parametrize("drug", [ASPIRIN, CIPROFLOXACIN])
def test_the_wording_is_prescriber_framed_and_claims_no_certainty(drug):
    """Critical Safety Rule #4. "Stop the aspirin" is a bug; "guidelines support considering" is
    the form, even where the finding is the most alarming one this table can produce."""
    summary = _flags(drug, 6)[0].summary
    assert "Guidelines support considering" in summary
    for imperative in ("Stop ", "Give ", "Administer ", "Switch the patient", "The patient has "):
        assert imperative not in summary


def test_the_aspirin_finding_names_the_syndrome_and_its_trigger():
    """Ungrounded, this is the system calling a common over-the-counter choice dangerous on no
    stated authority. The clinician's next question is "why", and the answer has to be on screen:
    the syndrome by name, and the viral illness that precedes it."""
    flag = _flags(ASPIRIN, 6)[0]
    assert "Reye" in flag.summary
    assert "viral illness" in flag.summary
    assert "BNF for Children" in flag.details["reference"]


def test_the_aspirin_finding_leaves_room_for_the_indication_that_justifies_it():
    """Aspirin is first-line in Kawasaki disease. A caution that read as "never in children"
    would be wrong on the one paediatric chart where the drug is unambiguously correct, and the
    clinician treating that child is precisely the one who cannot afford to start ignoring it."""
    assert "Kawasaki" in _flags(ASPIRIN, 6)[0].summary


def test_the_fluoroquinolone_finding_names_the_injury_and_the_exception():
    flag = _flags(CIPROFLOXACIN, 8)[0]
    assert "tendon" in flag.summary
    assert "no other agent is suitable" in flag.summary
    assert flag.details["reference"] == flag.details["reference"].strip()


def test_the_finding_reports_the_age_it_compared_against():
    """Both numbers, because the clinician is being asked to accept a conclusion drawn from the
    date of birth on the chart — which is a field this system read off a scan."""
    flag = _flags(ASPIRIN, 6)[0]
    assert "This patient is 6" in flag.summary
    assert flag.details["below_age_years"] == 16


# --- Fixed-dose combinations ----------------------------------------------------------------------


def test_a_combination_carries_its_components_caution():
    """A product's own class matches nothing, and the molecule the caution is about is a
    component of it — the same gap the geriatric side found with Glycomet GP."""
    combo = DrugRef(
        reference_id="ASP-CLO-75",
        generic_name="Aspirin + Clopidogrel",
        drug_class="Antiplatelet combination",
        components=(ASPIRIN, CLOPIDOGREL),
    )
    flags = check_paediatric_cautions(combo, SafetyContext(age_years=10))

    assert len(flags) == 1
    assert flags[0].details["proposed_drug"] == "Aspirin + Clopidogrel"
    assert flags[0].details["component"] == "Aspirin"


def test_one_caution_per_reference_even_if_two_identities_match_it():
    """Two fluoroquinolones under one prescription is one restriction, not two findings."""
    combo = DrugRef(
        reference_id="FQ-COMBO",
        generic_name="Ciprofloxacin + Levofloxacin",
        drug_class="Fluoroquinolone",
        components=(
            CIPROFLOXACIN,
            DrugRef(
                reference_id="LEV-500", generic_name="Levofloxacin", drug_class="Fluoroquinolone"
            ),
        ),
    )
    assert len(check_paediatric_cautions(combo, SafetyContext(age_years=10))) == 1


# --- Wiring ---------------------------------------------------------------------------------


def test_the_check_runs_as_part_of_the_standard_evaluation():
    flags = evaluate_drug_safety(ASPIRIN, SafetyContext(age_years=6))
    assert any(f.check_type == "paediatric_caution" for f in flags)


def test_the_check_type_is_one_the_audit_table_will_accept():
    """The flag is written to ``drug_safety_checks``, whose CHECK constraint enumerates the
    types. A finding the engine can produce and the table rejects is an insert that fails at the
    moment a critical caution is being recorded."""
    from app.models.drug_safety_check import DrugSafetyCheck

    constraint = next(c for c in DrugSafetyCheck.__table_args__ if c.name == "ck_dsc_check_type")
    assert "paediatric_caution" in str(constraint.sqltext)


def test_the_migration_widens_the_constraint_the_model_declares():
    """The model's constraint is what a fresh ``create_all`` builds; the migration is what an
    existing production database gets. Only one of those had been updated in the change that
    introduced ``bleeding_burden``'s predecessor, and the deployed database is the one that
    matters here."""
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[3]
        / "data"
        / "migrations"
        / "versions"
        / "0029_paediatric_caution_check.py"
    ).read_text(encoding="utf-8")
    assert "paediatric_caution" in migration


# --- Through the service, with a date of birth rather than an age -------------------------------


async def _patient_born(db, born: date | None):
    account, patient = await _account_and_patient(db)
    patient.date_of_birth = born
    await db.flush()
    return account, patient


@pytest.mark.asyncio
async def test_the_service_derives_the_age_and_the_flag_reaches_the_chart(db) -> None:
    """End to end on the axis that matters: the chart carries a date of birth, not an age."""
    account, patient = await _patient_born(db, date(datetime.now(UTC).year - 6, 1, 1))
    if await _vocab(db, "Aspirin") is None:
        pytest.skip("seed corpus lacks Aspirin")
    await _add_current_med(db, patient, "Aspirin")

    results = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    paediatric = [f for _v, g in results for f in g if f.check_type == "paediatric_caution"]

    assert len(paediatric) == 1
    assert paediatric[0].details["evaluated"] is True
    assert paediatric[0].details["age_years"] in (5, 6)
    assert paediatric[0].severity == "critical"


@pytest.mark.asyncio
async def test_a_record_with_no_date_of_birth_reaches_the_chart_as_unevaluated(db) -> None:
    """The branch that carries the most weight in this market: a child's paper record commonly
    records an age in years by hand rather than a date, so the population this check exists for
    is over-represented among the records that cannot supply a date of birth."""
    account, patient = await _patient_born(db, None)
    if await _vocab(db, "Aspirin") is None:
        pytest.skip("seed corpus lacks Aspirin")
    await _add_current_med(db, patient, "Aspirin")

    results = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    paediatric = [f for _v, g in results for f in g if f.check_type == "paediatric_caution"]

    assert len(paediatric) == 1
    assert paediatric[0].details["evaluated"] is False


@pytest.mark.asyncio
async def test_an_adult_chart_carrying_aspirin_stays_silent(db) -> None:
    """The negative case at service level, and the one that decides whether this check is
    tolerable at all: aspirin on an adult chart is routine, and a caution there would train the
    clinician to dismiss the badge before they ever meet the child it was written for."""
    account, patient = await _patient_born(db, date(datetime.now(UTC).year - 55, 1, 1))
    if await _vocab(db, "Aspirin") is None:
        pytest.skip("seed corpus lacks Aspirin")
    await _add_current_med(db, patient, "Aspirin")

    results = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    assert [f for _v, g in results for f in g if f.check_type == "paediatric_caution"] == []
