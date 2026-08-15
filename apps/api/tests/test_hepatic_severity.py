"""Child-Pugh and MELD: what the chart establishes about a liver, and what it leaves open.

``HepaticPanel`` deliberately stopped short of a Child-Pugh class last commit, and the reason
still holds — ascites and encephalopathy are graded by a clinician at the bedside and this record
holds neither, so a class asserted from labs alone is a confident-wrong number. What did not
follow is that nothing can be said. The two ungraded components contribute between 2 and 6 points
whatever they turn out to be, so three lab values fix the total inside a five-point window, and
for a sick enough liver that window lies entirely inside one class.

So the contract this file pins is: report the bounds, name a class only when the bounds agree,
and never fill a missing input.
"""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.core.hepatic import (
    ChildPughAssessment,
    assess_hepatic_severity,
    child_pugh_from_labs,
    meld_score,
)
from app.core.lab_safety import canonical_lab_value
from app.core.safety import HepaticPanel, SafetyContext, check_hepatic_severity
from app.models.condition import Condition
from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

pytestmark = pytest.mark.asyncio


def _cp(bilirubin: float | None, albumin: float | None, inr: float | None):
    return child_pugh_from_labs(bilirubin_mg_dl=bilirubin, albumin_g_dl=albumin, inr=inr)


# --- Child-Pugh component scoring ---------------------------------------------------------


@pytest.mark.parametrize(
    ("bilirubin", "expected"),
    [(0.8, 1), (1.99, 1), (2.0, 2), (3.0, 2), (3.01, 3), (18.0, 3)],
)
async def test_bilirubin_scores_on_the_published_bands(bilirubin: float, expected: int) -> None:
    assert _cp(bilirubin, 4.0, 1.0).component_points["bilirubin"] == expected


@pytest.mark.parametrize(
    ("albumin", "expected"),
    [(4.5, 1), (3.51, 1), (3.5, 2), (2.8, 2), (2.79, 3), (1.4, 3)],
)
async def test_albumin_scores_downward_not_upward(albumin: float, expected: int) -> None:
    """The one component where a *lower* number is the worse liver. Getting this backwards
    would score a dying liver as a healthy one and vice versa."""
    assert _cp(1.0, albumin, 1.0).component_points["albumin"] == expected


@pytest.mark.parametrize(
    ("inr", "expected"),
    [(1.0, 1), (1.69, 1), (1.7, 2), (2.3, 2), (2.31, 3), (5.0, 3)],
)
async def test_inr_scores_on_the_published_bands(inr: float, expected: int) -> None:
    assert _cp(1.0, 4.0, inr).component_points["inr"] == expected


# --- the bounded class ----------------------------------------------------------------------


async def test_a_healthy_panel_is_bounded_but_not_asserted_as_class_a() -> None:
    """Three perfect labs still leave 2-6 points of ungraded bedside components, which reaches
    9 — the top of class B. Claiming "Child-Pugh A" here would assert the absence of ascites
    and encephalopathy, which nothing on this chart establishes."""
    assessment = _cp(0.8, 4.2, 1.0)

    assert assessment.lab_points == 3
    assert (assessment.min_total, assessment.max_total) == (5, 9)
    assert (assessment.min_class, assessment.max_class) == ("A", "B")
    assert assessment.child_pugh_class is None
    assert assessment.is_determinate is False


async def test_the_worst_labs_are_class_c_however_the_bedside_grades() -> None:
    """9 lab points puts the floor at 11, and 11 is class C at any grading. This is the case the
    whole bounded design exists to reach: a determinate answer without a bedside examination."""
    assessment = _cp(8.0, 2.1, 3.4)

    assert assessment.lab_points == 9
    assert (assessment.min_total, assessment.max_total) == (11, 15)
    assert assessment.child_pugh_class == "C"
    assert assessment.is_determinate is True


async def test_eight_lab_points_is_the_threshold_where_the_class_becomes_determinate() -> None:
    """8 -> floor of 10, the first score that is class C. 7 -> floor of 9, still class B."""
    assert _cp(4.0, 2.1, 2.0).lab_points == 8
    assert _cp(4.0, 2.1, 2.0).child_pugh_class == "C"

    seven = _cp(4.0, 3.0, 2.0)
    assert seven.lab_points == 7
    assert seven.child_pugh_class is None
    assert (seven.min_class, seven.max_class) == ("B", "C")


@pytest.mark.parametrize(
    "missing",
    [
        {"bilirubin_mg_dl": None, "albumin_g_dl": 3.0, "inr": 1.5},
        {"bilirubin_mg_dl": 2.0, "albumin_g_dl": None, "inr": 1.5},
        {"bilirubin_mg_dl": 2.0, "albumin_g_dl": 3.0, "inr": None},
    ],
)
async def test_a_missing_component_produces_no_score_rather_than_a_partial_one(
    missing: dict,
) -> None:
    """Scoring two components and calling the third normal understates, and understates
    systematically: the marker a chart lacks is disproportionately the one nobody ordered."""
    assert child_pugh_from_labs(**missing) is None


async def test_the_ungraded_components_are_named_not_merely_absent() -> None:
    assert _cp(1.0, 4.0, 1.0).ungraded_components == ("ascites", "encephalopathy")


# --- MELD -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bilirubin", "inr", "creatinine", "expected"),
    [
        # 3.78·ln(2.0) + 11.2·ln(1.5) + 9.57·ln(1.4) + 6.43 = 16.81 -> 17
        (2.0, 1.5, 1.4, 17),
        # 3.78·ln(6.0) + 11.2·ln(2.1) + 9.57·ln(2.0) + 6.43 = 28.15 -> 28
        (6.0, 2.1, 2.0, 28),
    ],
)
async def test_meld_matches_a_worked_example(
    bilirubin: float, inr: float, creatinine: float, expected: int
) -> None:
    score = meld_score(bilirubin_mg_dl=bilirubin, inr=inr, creatinine_mg_dl=creatinine)

    assert score is not None
    assert score.score == expected


async def test_a_completely_normal_panel_is_the_meld_floor_of_six() -> None:
    score = meld_score(bilirubin_mg_dl=1.0, inr=1.0, creatinine_mg_dl=1.0)

    assert score is not None
    assert score.score == 6


async def test_values_below_one_are_floored_rather_than_subtracting_from_the_score() -> None:
    """The formula is a sum of logarithms, so an untouched bilirubin of 0.4 contributes a
    *negative* term and would quietly cancel a raised INR."""
    floored = meld_score(bilirubin_mg_dl=0.4, inr=0.9, creatinine_mg_dl=0.6)

    assert floored is not None
    assert floored.score == 6
    assert floored.inputs == {"bilirubin": 1.0, "inr": 1.0, "creatinine": 1.0}
    assert len(floored.clamped) == 3


async def test_a_creatinine_above_four_is_capped_and_says_so() -> None:
    """Above 4 the score stops measuring the liver. Two patients then produce the same number,
    which is only answerable because the clamp is reported."""
    capped = meld_score(bilirubin_mg_dl=3.0, inr=1.8, creatinine_mg_dl=9.0)
    at_cap = meld_score(bilirubin_mg_dl=3.0, inr=1.8, creatinine_mg_dl=4.0)

    assert capped is not None and at_cap is not None
    assert capped.score == at_cap.score
    assert capped.inputs["creatinine"] == 4.0
    assert any("capped" in c for c in capped.clamped)
    assert at_cap.clamped == ()


async def test_dialysis_substitutes_the_capped_creatinine_by_definition() -> None:
    on_dialysis = meld_score(bilirubin_mg_dl=3.0, inr=1.8, creatinine_mg_dl=1.1, on_dialysis=True)

    assert on_dialysis is not None
    assert on_dialysis.inputs["creatinine"] == 4.0
    assert any("dialysis" in c for c in on_dialysis.clamped)


async def test_meld_is_capped_at_forty() -> None:
    score = meld_score(bilirubin_mg_dl=60.0, inr=9.0, creatinine_mg_dl=4.0)

    assert score is not None
    assert score.score == 40


@pytest.mark.parametrize(
    "inputs",
    [
        {"bilirubin_mg_dl": None, "inr": 1.5, "creatinine_mg_dl": 1.0},
        {"bilirubin_mg_dl": 2.0, "inr": None, "creatinine_mg_dl": 1.0},
        {"bilirubin_mg_dl": 2.0, "inr": 1.5, "creatinine_mg_dl": None},
    ],
)
async def test_meld_needs_all_three_inputs_or_none_at_all(inputs: dict) -> None:
    assert meld_score(**inputs) is None


# --- the combined assessment ------------------------------------------------------------------


async def test_each_score_names_its_own_missing_inputs() -> None:
    """The two scores want overlapping-but-different inputs, so "MELD 18, no Child-Pugh — no
    albumin" is a thing that has to be sayable."""
    severity = assess_hepatic_severity(
        bilirubin_mg_dl=4.0, albumin_g_dl=None, inr=2.0, creatinine_mg_dl=1.2
    )

    assert severity.child_pugh is None
    assert severity.meld is not None
    assert severity.missing_child_pugh == ("serum albumin",)
    assert severity.missing_meld == ()
    assert bool(severity) is True


async def test_an_empty_chart_is_falsy_and_lists_everything_it_would_have_taken() -> None:
    severity = assess_hepatic_severity(
        bilirubin_mg_dl=None, albumin_g_dl=None, inr=None, creatinine_mg_dl=None
    )

    assert not severity
    assert severity.as_details() == {
        "child_pugh_missing_inputs": ["total bilirubin", "serum albumin", "INR"],
        "meld_missing_inputs": ["total bilirubin", "INR", "serum creatinine"],
    }


# --- the flag ---------------------------------------------------------------------------------


def _flags(**panel):
    return check_hepatic_severity(SafetyContext(hepatic=HepaticPanel(**panel)))


async def test_a_chart_establishing_neither_score_says_nothing() -> None:
    """Not the "silence reads as fine" trap: the dose-adjustment thresholds are evaluated on the
    same panel and do report themselves unevaluated. This is supplementary context, and an
    "unable to score" note on every chart in the country is how the real ones get ignored."""
    assert _flags(bilirubin_mg_dl=1.1) == []
    assert _flags() == []


async def test_a_determinate_class_c_is_critical_and_never_a_hard_block() -> None:
    """A liver score is not a contraindication. The contraindication rules are, and they run on
    the same panel."""
    flags = _flags(bilirubin_mg_dl=8.0, albumin_g_dl=2.1, inr=3.4)

    assert len(flags) == 1
    flag = flags[0]
    assert flag.check_type == "hepatic_severity"
    assert flag.severity == "critical"
    assert flag.is_hard_block is False
    assert flag.details["child_pugh_class"] == "C"
    assert "class C" in flag.summary


async def test_an_indeterminate_window_is_informational_and_states_the_range() -> None:
    """A warning on every panel whose labs merely *permit* class C would fire on a mildly
    abnormal chart, which is how a clinician learns to dismiss the determinate ones."""
    flags = _flags(bilirubin_mg_dl=2.5, albumin_g_dl=3.0, inr=1.8)

    assert len(flags) == 1
    flag = flags[0]
    assert flag.severity == "info"
    assert flag.details["child_pugh_class"] is None
    assert flag.details["class_range"] == ["B", "C"]
    assert "Child-Pugh B to C" in flag.summary
    assert "ascites and encephalopathy are graded" in flag.summary


async def test_a_meld_without_a_child_pugh_says_which_one_could_not_be_computed() -> None:
    flags = _flags(bilirubin_mg_dl=4.0, inr=2.0, creatinine_mg_dl=1.2)

    assert len(flags) == 1
    assert "No Child-Pugh score could be computed" in flags[0].summary
    assert "serum albumin" in flags[0].summary
    assert "MELD is" in flags[0].summary


async def test_the_flag_never_presents_meld_as_a_dosing_number() -> None:
    """MELD is a transplant-priority index. "Reduce the dose, MELD is 22" is not a thing."""
    flags = _flags(bilirubin_mg_dl=8.0, albumin_g_dl=2.1, inr=3.4, creatinine_mg_dl=2.0)

    assert "severity and referral index rather than a dosing one" in flags[0].summary


async def test_the_summary_never_uses_imperative_clinical_language() -> None:
    """CLAUDE.md safety rule #4 — prescriber-framed microcopy, checked on the text this
    generates rather than only on the templates a human wrote."""
    for panel in (
        {"bilirubin_mg_dl": 8.0, "albumin_g_dl": 2.1, "inr": 3.4},
        {"bilirubin_mg_dl": 2.5, "albumin_g_dl": 3.0, "inr": 1.8},
        {"bilirubin_mg_dl": 4.0, "inr": 2.0, "creatinine_mg_dl": 1.2},
    ):
        summary = _flags(**panel)[0].summary.lower()
        for banned in ("give ", "administer ", "the patient has ", "diagnose with ", "stop the "):
            assert banned not in summary, f"{banned!r} in {summary!r}"


# --- the panel the flag reads ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("marker", "value", "unit", "expected"),
    [
        ("Albumin", "3.4", "g/dL", 3.4),
        ("Serum Albumin", "3.4", "gm/dL", 3.4),
        ("ALB", "3.4", "g%", 3.4),
        ("Albumin", "34", "g/L", pytest.approx(3.4)),
    ],
)
async def test_albumin_is_read_however_an_indian_report_printed_it(
    marker: str, value: str, unit: str, expected
) -> None:
    assert canonical_lab_value(marker, float(value), unit) == ("albumin", expected)


@pytest.mark.parametrize(
    "marker",
    ["Urine Albumin", "Microalbumin", "Albumin/Creatinine Ratio", "Urine Microalbumin"],
)
async def test_a_urine_albumin_is_not_a_serum_albumin(marker: str) -> None:
    """R44's bug ("a urine creatinine was computed into an eGFR") wearing a different name. The
    marker table is matched exactly rather than by substring, which is what refuses these."""
    assert canonical_lab_value(marker, 30.0, "mg/L") is None
    assert canonical_lab_value(marker, 30.0, "g/dL") is None


async def test_a_urine_albumin_in_serum_units_is_still_refused() -> None:
    """The unit check is a second guard, not the only one — so neither alone has to be perfect."""
    assert canonical_lab_value("Urine Albumin", 3.4, "g/dL") is None


# --- through the service ----------------------------------------------------------------------


async def _patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"hepsev-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Hepatic Severity Patient",
        sex="male",
        date_of_birth=date(1970, 5, 4),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _lab(db, patient, marker, value, unit, when=date(2026, 3, 1)) -> None:
    db.add(
        LabResult(
            patient_id=patient.id,
            marker_name=marker,
            value_numeric=Decimal(str(value)),
            unit=unit,
            sample_date=when,
        )
    )
    await db.flush()


async def test_the_panel_carries_the_child_pugh_and_meld_inputs_off_the_chart(db) -> None:
    _, patient = await _patient(db)
    await _lab(db, patient, "Total Bilirubin", "4.1", "mg/dL")
    await _lab(db, patient, "Serum Albumin", "2.4", "g/dL")
    await _lab(db, patient, "INR", "2.6", "")
    await _lab(db, patient, "Serum Creatinine", "1.5", "mg/dL")

    panel = await SafetyService(db)._hepatic_panel(patient.id)

    assert panel.bilirubin_mg_dl == 4.1
    assert panel.albumin_g_dl == 2.4
    assert panel.inr == 2.6
    assert panel.creatinine_mg_dl == 1.5


async def test_a_urine_creatinine_on_the_chart_never_becomes_a_meld_input(db) -> None:
    """The predicate that fetches the row is deliberately loose; the predicate that accepts it
    is not. Feeding a urine creatinine to MELD is R44's failure in a second place."""
    _, patient = await _patient(db)
    await _lab(db, patient, "Urine Creatinine", "120", "mg/dL")

    panel = await SafetyService(db)._hepatic_panel(patient.id)

    assert panel.creatinine_mg_dl is None


async def test_a_liver_panel_of_only_an_albumin_does_not_pass_a_bilirubin_rule(db) -> None:
    """The panel grew from two markers to five, so "the panel is non-empty" stopped meaning "the
    marker this rule is written on was measured". A methotrexate rule keyed on bilirubin and ALT,
    against a chart carrying an albumin and nothing else, has to report itself unevaluated —
    otherwise the widening quietly reintroduced the exact bug the axis was added to fix."""
    account, patient = await _patient(db)
    await _lab(db, patient, "Serum Albumin", "2.4", "g/dL")

    _vocab, ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="MTX-7.5",
        drug_name=None,
    )

    assert bool(ctx.hepatic) is True
    hepatic = [f for f in flags if f.check_type == "hepatic_dose"]
    assert len(hepatic) == 1
    assert hepatic[0].details["evaluated"] is False


async def test_a_lone_creatinine_does_not_make_the_chart_look_like_it_has_lfts(db) -> None:
    """Every patient who has ever had a renal panel has a creatinine. Letting it make the panel
    truthy would turn "no LFTs on this chart" into "LFTs available" across the whole record."""
    _, patient = await _patient(db)
    await _lab(db, patient, "Serum Creatinine", "1.5", "mg/dL")

    panel = await SafetyService(db)._hepatic_panel(patient.id)

    assert panel.creatinine_mg_dl == 1.5
    assert not panel


async def test_the_flags_endpoint_reports_the_severity_once_for_the_chart(db) -> None:
    account, patient = await _patient(db)
    await _lab(db, patient, "Total Bilirubin", "8.0", "mg/dL")
    await _lab(db, patient, "Serum Albumin", "2.1", "g/dL")
    await _lab(db, patient, "INR", "3.4", "")

    flags = await SafetyService(db).chart_completeness_flags(patient.id)

    severity = [f for f in flags if f.check_type == "hepatic_severity"]
    assert len(severity) == 1
    assert severity[0].details["child_pugh_class"] == "C"
    assert account is not None


async def test_the_drug_check_reports_the_severity_too_because_nothing_else_reaches_a_clinician(
    db,
) -> None:
    """The Child-Pugh assessment was computed, persisted and served — on an endpoint with no
    caller. ``chart_completeness_flags`` is reached only from ``GET ../flags``, and the Safety
    screen calls ``POST ../check`` and nothing else; so a decompensated liver was established
    from this chart's own labs and shown to nobody.

    Which makes it the same defect as the four before it in a new place — a check that ran,
    reported into a void. It is appended in ``check_medication`` rather than folded into
    ``evaluate_drug_safety`` because it is a statement about the chart: the callers that
    evaluate many drugs against one context must not repeat it per drug, and this one evaluates
    exactly one.
    """
    account, patient = await _patient(db)
    await _lab(db, patient, "Total Bilirubin", "8.0", "mg/dL")
    await _lab(db, patient, "Serum Albumin", "2.1", "g/dL")
    await _lab(db, patient, "INR", "3.4", "")

    _vocab, _ctx, flags, check_ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="MTX-7.5",
        drug_name=None,
    )

    severity = [f for f in flags if f.check_type == "hepatic_severity"]
    assert len(severity) == 1
    assert severity[0].details["child_pugh_class"] == "C"
    # Persisted like every other flag this path returns, so the record of what was shown holds
    # the liver state it was shown beside.
    assert len(check_ids) == len(flags)


async def test_the_drug_check_reports_an_unreadable_problem_list_row(db) -> None:
    """Its sibling on the same path, and the reason both are appended in one place: a condition
    the tokeniser cannot read was compared to no contraindication rule at all, and the Safety
    screen would otherwise render that as a clean chart."""
    account, patient = await _patient(db)
    db.add(Condition(patient_id=patient.id, condition_name="गर्भावस्था", status="active"))
    await db.flush()

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="MTX-7.5",
        drug_name=None,
    )

    unevaluated = [f for f in flags if f.check_type == "unevaluated_condition"]
    assert len(unevaluated) == 1
    assert unevaluated[0].is_hard_block is False
    assert unevaluated[0].details["unevaluated_conditions"] == ["गर्भावस्था"]


async def test_a_chart_level_flag_is_not_repeated_once_per_current_medication(db) -> None:
    """The constraint that keeps the append in ``check_medication`` honest. ``active_flags``
    evaluates every current medication against one context; folding these into
    ``evaluate_drug_safety`` would print the same Child-Pugh sentence once per drug, and a
    clinician on eight medications would scroll past eight copies of it to find the block."""
    account, patient = await _patient(db)
    await _lab(db, patient, "Total Bilirubin", "8.0", "mg/dL")
    await _lab(db, patient, "Serum Albumin", "2.1", "g/dL")
    await _lab(db, patient, "INR", "3.4", "")

    results = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)

    per_drug = [f for _vocab, flag_list in results for f in flag_list]
    assert [f for f in per_drug if f.check_type == "hepatic_severity"] == []


async def test_the_assessment_is_a_frozen_value_not_a_mutable_one() -> None:
    """It ends up in a persisted flag's details; a caller mutating it after the fact would make
    the record disagree with what was shown."""
    assessment = _cp(1.0, 4.0, 1.0)

    assert isinstance(assessment, ChildPughAssessment)
    with pytest.raises(FrozenInstanceError):
        assessment.lab_points = 9  # type: ignore[misc]


async def test_an_inr_printed_as_pt_inr_still_reaches_both_scores(db) -> None:
    """The name a coagulation panel actually prints, end to end into Child-Pugh and MELD.

    ``pt inr`` was missing from the marker alias table while the long "prothrombin time inr" form
    was present, so the commonest printed spelling did not resolve and the row was skipped. The
    INR is the one input *both* scores need, so the chart lost the whole hepatic severity picture
    at once — and reported the INR as an input it did not carry, on a chart that carried it.
    Nothing raised and nothing was fabricated, which is why it went unnoticed.
    """
    _, patient = await _patient(db)
    await _lab(db, patient, "Total Bilirubin", "4.1", "mg/dL")
    await _lab(db, patient, "Serum Albumin", "2.4", "g/dL")
    await _lab(db, patient, "PT/INR", "2.6", "")
    await _lab(db, patient, "Serum Creatinine", "1.5", "mg/dL")

    panel = await SafetyService(db)._hepatic_panel(patient.id)
    assert panel.inr == 2.6

    severity = assess_hepatic_severity(
        bilirubin_mg_dl=panel.bilirubin_mg_dl,
        albumin_g_dl=panel.albumin_g_dl,
        inr=panel.inr,
        creatinine_mg_dl=panel.creatinine_mg_dl,
    )
    assert severity.child_pugh is not None
    assert severity.meld is not None
    assert severity.missing_child_pugh == ()
    assert severity.missing_meld == ()


async def test_a_prothrombin_time_in_seconds_is_not_read_as_an_inr(db) -> None:
    """The fence on the widening above. PT is ~11-14 seconds; read as an INR it is Child-Pugh C.

    A chart with a normal clotting screen and no INR must come back with no INR — which is what
    ``missing_child_pugh`` is for — rather than with a fabricated coagulopathy.
    """
    _, patient = await _patient(db)
    await _lab(db, patient, "Total Bilirubin", "4.1", "mg/dL")
    await _lab(db, patient, "Serum Albumin", "2.4", "g/dL")
    await _lab(db, patient, "Prothrombin Time", "13.2", "sec")

    panel = await SafetyService(db)._hepatic_panel(patient.id)
    assert panel.inr is None

    severity = assess_hepatic_severity(
        bilirubin_mg_dl=panel.bilirubin_mg_dl,
        albumin_g_dl=panel.albumin_g_dl,
        inr=panel.inr,
        creatinine_mg_dl=panel.creatinine_mg_dl,
    )
    assert severity.child_pugh is None
    assert "INR" in severity.missing_child_pugh
