"""A charted dose measured against the therapeutic range for the drug it is a dose of.

``app.core.dose_text`` judged whether a number in *model-written prose* could be a dose of the
named product at all, against a deliberately enormous 100x ceiling, and said in its own
docstring that it attempted no judgement about whether a dose was right for a patient "because
that needs indication, weight, renal function and a curated per-drug maximum".

Nothing checked what a clinician typed, or what an OCR pass read off a prescription. So this
system would chart "Levothyroxine 100 mg once daily" — a thousand times the dose, and a
perfectly fluent line — run every allergy, interaction and contraindication rule against it,
find nothing, and present the chart as checked.

Three prescribing errors, in the order they kill people:

* **the wrong unit** — levothyroxine and digoxin are dosed in micrograms;
* **the wrong interval** — methotrexate charted daily rather than weekly;
* **the right dose for the wrong body** — an adult ceiling on a nine-year-old, or a metformin
  dose that was correct before this patient's eGFR fell to 38.

None of the findings is a hard block, and the tests below pin that in both directions. Dose
ceilings are exceeded deliberately and routinely by specialists for reasons this record does not
hold; spending Rule #3's instrument on "probably wrong" is how the blocks that are never wrong
stop being read.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.dose_range import (
    THERAPEUTIC_RANGES,
    DoseRange,
    assess_dose,
    doses_per_day,
    is_daily_frequency,
    parse_charted_amount,
    range_for,
)
from app.core.safety import (
    ChartedDose,
    DrugRef,
    SafetyContext,
    check_dose_ranges,
    check_proposed_dose,
    evaluate_drug_safety,
)
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

pytestmark = pytest.mark.asyncio


def _kinds(assessments) -> list[str]:
    return [a.kind for a in assessments]


# --- reading a charted amount -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("dose", "unit", "expected_mg"),
    [
        ("500", "mg", 500.0),
        ("500 mg", None, 500.0),
        ("500mg", None, 500.0),
        ("0.25", "mg", 0.25),
        ("100 mcg", None, 0.1),
        ("100 µg", None, 0.1),
        ("1 g", None, 1000.0),
        ("1,000 mg", None, 1000.0),
        # A range is judged at its upper end, exactly as prose ranges are.
        ("500-1000 mg", None, 1000.0),
        ("1 to 2 g", None, 2000.0),
        # The unit written inside ``dose`` wins over the structured column.
        ("100 mcg", "mg", 0.1),
    ],
)
async def test_a_charted_amount_reduces_to_a_mass(dose, unit, expected_mg) -> None:
    parsed = parse_charted_amount(dose, unit)

    assert parsed is not None
    assert parsed.milligrams == pytest.approx(expected_mg)


@pytest.mark.parametrize(
    "dose",
    [
        None,
        "",
        "   ",
        # A tablet count is a quantity of *product*, and converting it would need the dispensed
        # strength, which the medication row does not carry. Nothing is flagged from one.
        "1 tablet",
        "2 tabs",
        "2 puffs",
        "1 cap",
        "as directed",
        "1 tsp",
    ],
)
async def test_a_quantity_that_names_no_mass_is_not_guessed_at(dose) -> None:
    assert parse_charted_amount(dose, None) is None


async def test_a_zero_dose_is_not_a_dose() -> None:
    assert parse_charted_amount("0 mg", None) is None


# --- reading a frequency ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frequency", "expected"),
    [
        ("OD", 1),
        ("od", 1),
        ("once daily", 1),
        ("BD", 2),
        ("bid", 2),
        ("twice daily", 2),
        ("TDS", 3),
        ("three times a day", 3),
        ("QDS", 4),
        ("q8h", 3),
        ("q 6 h", 4),
        ("every 12 hours", 2),
        ("8 hourly", 3),
        ("HS", 1),
        ("alternate days", 0.5),
        ("once weekly", 1 / 7),
        # The Indian morning-afternoon-night grid, which no formulary lists and nearly every
        # prescription pad in this market uses.
        ("1-0-1", 2),
        ("1-1-1", 3),
        ("0-0-1", 1),
        ("1/0/1", 2),
    ],
)
async def test_a_frequency_reduces_to_administrations_a_day(frequency, expected) -> None:
    assert doses_per_day(frequency) == pytest.approx(expected)


@pytest.mark.parametrize(
    "frequency",
    [
        None,
        "",
        # As-needed has no daily total by construction: how many doses is the patient's call.
        "SOS",
        "PRN",
        "as needed",
        "when required",
        # A spelling this function does not know is "it does not say", not "once".
        "with meals",
        "1-0-1-0-1",
    ],
)
async def test_an_unreadable_frequency_is_never_read_as_once_daily(frequency) -> None:
    """Assuming the smallest frequency would clear every overdose beside an unreadable one."""
    assert doses_per_day(frequency) is None


@pytest.mark.parametrize("frequency", ["OD", "BD", "1-0-1", "q8h"])
async def test_a_daily_frequency_is_recognised_as_daily(frequency) -> None:
    assert is_daily_frequency(frequency) is True


@pytest.mark.parametrize("frequency", [None, "", "once weekly", "alternate days", "SOS"])
async def test_a_non_daily_or_unreadable_frequency_is_not_called_daily(frequency) -> None:
    assert is_daily_frequency(frequency) is False


# --- the unit slip ------------------------------------------------------------------------------


async def test_levothyroxine_in_milligrams_is_reported_as_a_unit_slip() -> None:
    """The flagship case: 1000x the dose, and a fluent line on a prescription."""
    findings = assess_dose(generic_name="Levothyroxine", dose="100", dose_unit="mg", frequency="OD")

    assert _kinds(findings) == ["unit_mismatch"]
    finding = findings[0]
    assert finding.severe is True
    assert finding.details["charted_unit"] == "mg"
    assert finding.details["expected_unit"] == "mcg"
    assert "100 mcg is an ordinary one" in finding.message


async def test_digoxin_in_milligrams_at_a_microgram_number_is_a_unit_slip() -> None:
    findings = assess_dose(generic_name="Digoxin", dose="250", dose_unit="mg", frequency="OD")

    assert _kinds(findings) == ["unit_mismatch"]


async def test_a_milligram_drug_charted_in_micrograms_is_a_unit_slip_too() -> None:
    """The slip runs both ways: 500 mcg of metformin is a thousandth of a dose."""
    findings = assess_dose(generic_name="Metformin", dose="500", dose_unit="mcg", frequency="BD")

    assert _kinds(findings) == ["unit_mismatch"]
    assert findings[0].details["expected_unit"] == "mg"


async def test_a_genuine_overdose_is_not_reported_as_a_typing_error() -> None:
    """The slip test needs both halves: out of range as charted *and* in range reinterpreted.

    9000 mg of metformin is a genuine overdose written in the drug's own unit. There is no
    other unit to reinterpret it in, so it comes back as the overdose it is rather than as a
    typing error nobody made — which matters because the two findings ask the clinician to do
    different things (confirm the number, versus go and re-read the source line).
    """
    findings = assess_dose(generic_name="Metformin", dose="9000", dose_unit="mg", frequency="OD")

    assert "unit_mismatch" not in _kinds(findings)
    assert "above_maximum" in _kinds(findings)


async def test_a_dose_in_the_drugs_own_unit_is_never_a_unit_slip() -> None:
    findings = assess_dose(
        generic_name="Levothyroxine", dose="5000", dose_unit="mcg", frequency="OD"
    )

    assert "unit_mismatch" not in _kinds(findings)
    assert "above_maximum" in _kinds(findings)


# --- the wrong interval ---------------------------------------------------------------------------


async def test_methotrexate_charted_daily_is_the_finding_that_matters() -> None:
    findings = assess_dose(
        generic_name="Methotrexate", dose="7.5 mg", dose_unit=None, frequency="OD"
    )

    assert _kinds(findings) == ["interval_mismatch"]
    assert findings[0].severe is True
    assert "fatal marrow suppression" in findings[0].message


async def test_methotrexate_charted_weekly_raises_nothing() -> None:
    assert (
        assess_dose(
            generic_name="Methotrexate", dose="15 mg", dose_unit=None, frequency="once weekly"
        )
        == []
    )


async def test_methotrexate_with_an_unreadable_frequency_raises_no_interval_flag() -> None:
    """An untranscribable frequency is not evidence the drug was taken daily."""
    findings = assess_dose(
        generic_name="Methotrexate", dose="15 mg", dose_unit=None, frequency="as directed"
    )

    assert "interval_mismatch" not in _kinds(findings)


async def test_the_interval_finding_is_not_stacked_with_a_dose_one() -> None:
    """One flag to act on, not a second weaker one underneath it."""
    findings = assess_dose(
        generic_name="Methotrexate", dose="20 mg", dose_unit=None, frequency="1-0-1"
    )

    assert _kinds(findings) == ["interval_mismatch"]


# --- above and below the range --------------------------------------------------------------------


async def test_a_daily_total_above_the_adult_maximum_is_flagged() -> None:
    findings = assess_dose(
        generic_name="Metformin", dose="1000 mg", dose_unit=None, frequency="TDS"
    )

    assert _kinds(findings) == ["above_maximum"]
    finding = findings[0]
    assert finding.details["charted_daily_total_mg"] == pytest.approx(3000)
    assert finding.details["maximum_daily_mg"] == pytest.approx(2550)
    assert finding.severe is False, "30% over is the ordinary shape of specialist prescribing"


async def test_double_the_maximum_grades_the_finding_critical() -> None:
    findings = assess_dose(generic_name="Amlodipine", dose="20 mg", dose_unit=None, frequency="BD")

    assert findings[0].severe is True


async def test_a_dose_inside_the_range_raises_nothing() -> None:
    assert (
        assess_dose(generic_name="Metformin", dose="500 mg", dose_unit=None, frequency="BD") == []
    )


async def test_a_sub_therapeutic_daily_total_is_noted_and_not_questioned() -> None:
    findings = assess_dose(generic_name="Metformin", dose="100 mg", dose_unit=None, frequency="OD")

    assert _kinds(findings) == ["below_minimum"]
    assert findings[0].severe is False
    assert "may be intentional" in findings[0].message


async def test_an_unreadable_frequency_still_checks_the_single_dose() -> None:
    """No daily total is computable, but a single dose ceiling is an upper bound regardless."""
    findings = assess_dose(
        generic_name="Paracetamol", dose="4000 mg", dose_unit=None, frequency="as directed"
    )

    assert _kinds(findings) == ["above_maximum"]
    assert findings[0].details["basis"] == "single dose"


async def test_an_as_needed_order_is_not_given_an_invented_daily_total() -> None:
    findings = assess_dose(
        generic_name="Paracetamol", dose="1000 mg", dose_unit=None, frequency="SOS"
    )

    assert findings == []


# --- the patient the dose is for ------------------------------------------------------------------


async def test_a_renal_ceiling_replaces_the_adult_one_and_says_so() -> None:
    findings = assess_dose(
        generic_name="Metformin", dose="1000 mg", dose_unit=None, frequency="BD", egfr=38
    )

    assert _kinds(findings) == ["above_maximum"]
    finding = findings[0]
    assert finding.details["maximum_daily_mg"] == pytest.approx(1000)
    assert "eGFR of 38" in finding.message


async def test_the_same_dose_passes_at_a_normal_egfr() -> None:
    assert (
        assess_dose(
            generic_name="Metformin", dose="1000 mg", dose_unit=None, frequency="BD", egfr=95
        )
        == []
    )


async def test_a_chart_with_no_egfr_takes_the_adult_ceiling() -> None:
    """Not the renal one: a threshold applied to an eGFR nobody measured is a guess."""
    findings = assess_dose(generic_name="Metformin", dose="1000 mg", dose_unit=None, frequency="BD")

    assert findings == []


async def test_a_geriatric_ceiling_binds_where_one_is_curated() -> None:
    findings = assess_dose(
        generic_name="Spironolactone", dose="50 mg", dose_unit=None, frequency="OD", age_years=78
    )

    assert _kinds(findings) == ["above_maximum"]
    assert findings[0].details["maximum_daily_mg"] == pytest.approx(25)
    assert "at 78 years" in findings[0].message


async def test_the_tighter_of_the_two_adjustments_wins() -> None:
    """An older adult with poor renal function is entitled to both, and to the stricter."""
    findings = assess_dose(
        generic_name="Digoxin",
        dose="0.25 mg",
        dose_unit=None,
        frequency="OD",
        age_years=80,
        egfr=25,
    )

    assert findings[0].details["maximum_daily_mg"] == pytest.approx(0.125)


async def test_a_child_on_a_weight_dosed_drug_with_no_weight_is_reported_as_unchecked() -> None:
    findings = assess_dose(
        generic_name="Paracetamol", dose="500 mg", dose_unit=None, frequency="1-1-1", age_years=8
    )

    assert _kinds(findings) == ["not_evaluated"]
    assert findings[0].details["evaluated"] is False
    assert "not the same as a dose within range" in findings[0].message


async def test_a_child_with_a_weight_is_judged_per_kilogram() -> None:
    findings = assess_dose(
        generic_name="Paracetamol",
        dose="500 mg",
        dose_unit=None,
        frequency="1-1-1",
        age_years=8,
        weight_kg=20,
    )

    assert _kinds(findings) == ["above_maximum"]
    finding = findings[0]
    assert finding.details["charted_mg_per_kg_per_day"] == pytest.approx(75)
    assert finding.details["maximum_mg_per_kg_per_day"] == pytest.approx(60)
    assert finding.details["weight_kg"] == 20


async def test_a_correct_paediatric_dose_raises_nothing() -> None:
    assert (
        assess_dose(
            generic_name="Paracetamol",
            dose="250 mg",
            dose_unit=None,
            frequency="1-1-1",
            age_years=8,
            weight_kg=20,
        )
        == []
    )


async def test_an_adult_ceiling_is_never_applied_to_a_child() -> None:
    """Wrong in both directions — it clears a dangerous dose and invents a sub-therapeutic one."""
    adult = assess_dose(generic_name="Metformin", dose="100 mg", dose_unit=None, frequency="OD")
    child = assess_dose(
        generic_name="Metformin", dose="100 mg", dose_unit=None, frequency="OD", age_years=6
    )

    assert _kinds(adult) == ["below_minimum"]
    assert child == [], "metformin has no curated paediatric band, so nothing is claimed"


async def test_an_adolescent_takes_the_adult_range() -> None:
    findings = assess_dose(
        generic_name="Metformin", dose="1500 mg", dose_unit=None, frequency="TDS", age_years=15
    )

    # Both ceilings bind: 1500 mg is over the single-dose maximum and 4500 mg/day is over the
    # daily one. Two findings rather than one because they are two different confirmations.
    assert _kinds(findings) == ["above_maximum", "above_maximum"]
    assert {f.details["basis"] for f in findings} == {"single dose", "daily total"}


# --- what the table does and does not claim ------------------------------------------------------


async def test_a_drug_with_no_curated_range_is_not_reported_as_within_range() -> None:
    """None from ``range_for`` is "not curated", never "no maximum"."""
    assert range_for("Ceftazidime") is None
    assert (
        assess_dose(generic_name="Ceftazidime", dose="10 g", dose_unit=None, frequency="TDS") == []
    )


async def test_combination_products_are_absent_from_the_table() -> None:
    """A charted number cannot be apportioned between two molecules, so none is attempted."""
    for name in THERAPEUTIC_RANGES:
        assert "+" not in name, name


async def test_every_curated_range_is_internally_consistent() -> None:
    """A ceiling below its own floor would flag every correct dose of that drug at both ends."""
    for name, entry in THERAPEUTIC_RANGES.items():
        assert entry.dosing_unit in {"mg", "mcg"}, name
        assert entry.single_max_mg > 0, name
        assert entry.daily_max_mg >= entry.single_max_mg or entry.interval == "weekly", name
        if entry.daily_min_mg is not None:
            assert entry.daily_min_mg <= entry.daily_max_mg, name
        for threshold, renal_max in entry.renal_daily_max_mg:
            assert threshold > 0 and 0 < renal_max <= entry.daily_max_mg, name
        if entry.geriatric_daily_max_mg is not None:
            assert 0 < entry.geriatric_daily_max_mg <= entry.daily_max_mg, name
        if entry.paediatric_mg_per_kg_per_day is not None:
            low, high = entry.paediatric_mg_per_kg_per_day
            assert 0 < low <= high, name


async def test_the_table_is_keyed_on_normalised_generic_names() -> None:
    for name in THERAPEUTIC_RANGES:
        assert name == name.strip().lower(), name


@pytest.mark.parametrize("name", ["metformin", "METFORMIN", "  Metformin  "])
async def test_lookup_is_case_and_space_insensitive(name) -> None:
    assert isinstance(range_for(name), DoseRange)


# --- as safety flags ----------------------------------------------------------------------------


async def test_the_chart_level_check_reports_each_charted_dose_once() -> None:
    ctx = SafetyContext(
        charted_doses=[
            ChartedDose(
                drug=DrugRef("LT4-100", "Levothyroxine", "Thyroid hormone"),
                dose="100",
                dose_unit="mg",
                frequency="OD",
            ),
            ChartedDose(
                drug=DrugRef("MET-500", "Metformin", "Biguanide"),
                dose="1000 mg",
                frequency="TDS",
            ),
        ]
    )

    flags = check_dose_ranges(ctx)

    assert [f.check_type for f in flags] == ["dose_unit_mismatch", "dose_out_of_range"], (
        "ordered by drug name so the screen does not reshuffle between refreshes"
    )
    assert [f.severity for f in flags] == ["critical", "warning"]
    assert all(f.is_hard_block is False for f in flags)
    assert flags[0].details["reference_id"] == "LT4-100"


async def test_no_dose_finding_is_ever_a_hard_block() -> None:
    """Rule #3's instrument is for a documented conflict, not for a probably-wrong number."""
    ctx = SafetyContext(
        charted_doses=[
            ChartedDose(
                drug=DrugRef("MTX-7.5", "Methotrexate", "Antimetabolite (DMARD)"),
                dose="25 mg",
                frequency="1-1-1",
            )
        ]
    )

    flags = check_dose_ranges(ctx)

    assert flags and all(f.is_hard_block is False for f in flags)
    assert all(f.severity != "hard_block" for f in flags)


async def test_the_chart_level_check_is_not_repeated_once_per_drug() -> None:
    """It is a statement about the medication list, so ``evaluate_drug_safety`` must not run it."""
    ctx = SafetyContext(
        charted_doses=[
            ChartedDose(
                drug=DrugRef("LT4-100", "Levothyroxine"), dose="100", dose_unit="mg", frequency="OD"
            )
        ]
    )

    assert evaluate_drug_safety(DrugRef("PCM-500", "Paracetamol", "Analgesic"), ctx) == []


async def test_a_combination_product_is_evaluated_as_its_ingredients() -> None:
    """The engine's standing rule: every check runs against molecules, not against products."""
    combination = DrugRef(
        "MET-GLM-1-500",
        "Metformin + Glimepiride",
        "Biguanide + Sulfonylurea",
        components=(DrugRef("MET-500", "Metformin"), DrugRef("GLM-1", "Glimepiride")),
    )
    ctx = SafetyContext(
        charted_doses=[ChartedDose(drug=combination, dose="3000 mg", frequency="OD")]
    )

    flags = check_dose_ranges(ctx)

    assert {f.details["drug"] for f in flags} == {"Metformin", "Glimepiride"}


async def test_a_proposed_dose_is_checked_and_an_absent_one_is_not() -> None:
    ctx = SafetyContext()
    proposed = DrugRef("LT4-100", "Levothyroxine", "Thyroid hormone")

    assert check_proposed_dose(proposed, ctx, dose=None) == []
    assert check_proposed_dose(proposed, ctx, dose="   ") == []
    flagged = check_proposed_dose(proposed, ctx, dose="100", dose_unit="mg", frequency="OD")
    assert [f.check_type for f in flagged] == ["dose_unit_mismatch"]


async def test_the_proposed_dose_check_reads_the_patient_from_the_context() -> None:
    ctx = SafetyContext(age_years=80, egfr=25)

    flags = check_proposed_dose(
        DrugRef("DIG-0.25", "Digoxin", "Cardiac glycoside"),
        ctx,
        dose="0.25 mg",
        frequency="OD",
    )

    assert [f.check_type for f in flags] == ["dose_out_of_range"]
    assert flags[0].details["maximum_daily_mg"] == pytest.approx(0.125)


# --- through the service --------------------------------------------------------------------------


async def _account_and_patient(db, **patient_fields) -> tuple[Account, Patient]:
    account = Account(email=f"dose-range-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    fields = {
        "full_name": "Dose Range Patient",
        "sex": "male",
        "date_of_birth": datetime(1970, 1, 1).date(),
        "consent_given": True,
        "consent_given_at": datetime.now(UTC),
    }
    fields.update(patient_fields)
    patient = Patient(account_id=account.id, **fields)
    db.add(patient)
    await db.flush()
    return account, patient


async def _chart(db, patient: Patient, **fields) -> None:
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            event_type="start",
            is_current=True,
            **fields,
        )
    )
    await db.flush()


async def test_the_service_carries_the_charted_dose_into_the_context(db) -> None:
    _, patient = await _account_and_patient(db)
    await _chart(db, patient, generic_name="Metformin", dose="500", dose_unit="mg", frequency="BD")

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert [(c.drug.generic_name, c.dose, c.dose_unit, c.frequency) for c in ctx.charted_doses] == [
        ("Metformin", "500", "mg", "BD")
    ]


async def test_a_medication_row_nothing_resolves_carries_no_charted_dose(db) -> None:
    """It has no drug to look a range up for, and is already reported as unevaluated."""
    _, patient = await _account_and_patient(db)
    await _chart(
        db, patient, brand_name_raw="Zyxolol 40", dose="40", dose_unit="mg", frequency="OD"
    )

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert ctx.charted_doses == []
    assert ctx.unresolved_current_meds == ["Zyxolol 40"]


async def test_the_service_carries_the_recorded_weight(db) -> None:
    _, patient = await _account_and_patient(db, weight_kg=18.5)

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert ctx.weight_kg == pytest.approx(18.5)


async def test_a_chart_with_no_weight_reports_none_rather_than_zero(db) -> None:
    _, patient = await _account_and_patient(db)

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert ctx.weight_kg is None


async def test_a_dangerous_charted_dose_reaches_the_standing_safety_board(db) -> None:
    """End to end: the row a clinician can see, and the flag they now get about it."""
    _, patient = await _account_and_patient(db)
    await _chart(
        db, patient, generic_name="Levothyroxine", dose="100", dose_unit="mg", frequency="OD"
    )

    flags = await SafetyService(db).chart_completeness_flags(patient.id)

    unit_flags = [f for f in flags if f.check_type == "dose_unit_mismatch"]
    assert len(unit_flags) == 1
    assert unit_flags[0].severity == "critical"
    assert unit_flags[0].is_hard_block is False


# --- over the wire --------------------------------------------------------------------------------


async def test_a_proposed_dose_can_be_checked_before_it_is_prescribed(auth_client) -> None:
    from .conftest import create_patient

    patient = await create_patient(auth_client)

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Levothyroxine", "dose": "100", "dose_unit": "mg", "frequency": "OD"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "dose_unit_mismatch" in {f["check_type"] for f in body["flags"]}
    # A dose finding must not block the prescription; it routes nothing through the override
    # path, because there is nothing here a clinician cannot simply be right about.
    assert body["is_blocked"] is False


async def test_omitting_the_dose_checks_the_drug_and_says_nothing_about_a_dose(auth_client) -> None:
    from .conftest import create_patient

    patient = await create_patient(auth_client)

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Levothyroxine"},
    )

    assert resp.status_code == 200, resp.text
    types = {f["check_type"] for f in resp.json()["flags"]}
    assert not types & {"dose_out_of_range", "dose_unit_mismatch", "unevaluated_dose"}


async def test_a_weight_can_be_recorded_and_read_back(auth_client) -> None:
    from .conftest import create_patient

    patient = await create_patient(auth_client, weight_kg=18.5)
    assert patient["weight_kg"] == pytest.approx(18.5)
    assert patient["weight_recorded_at"] is not None

    resp = await auth_client.patch(f"/api/v1/patients/{patient['id']}", json={"weight_kg": 20.25})

    assert resp.status_code == 200, resp.text
    assert resp.json()["weight_kg"] == pytest.approx(20.25)


async def test_clearing_a_weight_clears_the_date_it_was_taken(auth_client) -> None:
    """A timestamp left behind would date a measurement that no longer exists."""
    from .conftest import create_patient

    patient = await create_patient(auth_client, weight_kg=70)

    resp = await auth_client.patch(f"/api/v1/patients/{patient['id']}", json={"weight_kg": None})

    assert resp.status_code == 200, resp.text
    assert resp.json()["weight_kg"] is None
    assert resp.json()["weight_recorded_at"] is None


@pytest.mark.parametrize("weight", [0, -5, 900, 0.01])
async def test_a_weight_the_arithmetic_cannot_use_is_refused(auth_client, weight) -> None:
    """Weight is a divisor. A zero produces an infinite mg/kg figure."""
    resp = await auth_client.post(
        "/api/v1/patients",
        json={"full_name": "Weight Bound", "consent_given": True, "weight_kg": weight},
    )

    assert resp.status_code == 422, resp.text
