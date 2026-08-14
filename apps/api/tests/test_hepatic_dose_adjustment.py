"""The hepatic dose-adjustment axis, which was declared everywhere and evaluated nowhere.

``hepatic_threshold`` has been a column on the contraindication table, a documented field in
``data/drugs/schema.json``, and a value ``SafetyService`` loads into ``ContraindicationRule``
since the table was written. ``hepatic_dose`` has been a permitted ``check_type`` in the
database constraint, in the shared TypeScript enums and in ``app.core.safety.CheckType``. No
code read either. A curated rule keyed on a hepatic threshold loaded, matched no condition
name, and fell out of the loop silently — so the drug came back with a clean check because the
only rule bearing on it was written on an axis with no evaluator.

The same shape as the renal rule that reported itself as having passed (R43), the failed
Verifier that reported itself as agreeing (R38), and the unreadable medication line reported as
a clean chart (R44).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.safety import (
    ContraindicationRule,
    DrugRef,
    HepaticPanel,
    PatientCondition,
    SafetyContext,
    check_contraindications,
)
from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

pytestmark = pytest.mark.asyncio

_MTX = DrugRef("MTX-7.5", "Methotrexate", "Antimetabolite (DMARD)")

# The shipped methotrexate rule: hepatotoxicity is its dose-limiting toxicity, so the action is
# a hard block, reached from either marker.
_MTX_RULE = ContraindicationRule(
    drug_reference_id="MTX-7.5",
    condition_name="Significant Hepatic Impairment",
    severity="dose_adjustment_required",
    description="Methotrexate is hepatotoxic",
    is_absolute=False,
    hepatic_threshold={"bilirubin_above": 3.0, "alt_above": 120, "action": "contraindicated"},
    contraindication_id=str(uuid.uuid4()),
)

# A dose-adjustment rule rather than a block, on one marker only.
_PCM = DrugRef("PCM-650", "Paracetamol", "Analgesic")
_PCM_RULE = ContraindicationRule(
    drug_reference_id="PCM-650",
    condition_name="Hepatic Impairment",
    severity="dose_adjustment_required",
    description="Maximum daily dose is reduced in hepatic impairment",
    is_absolute=False,
    hepatic_threshold={"bilirubin_above": 3.0, "action": "reduce_max_daily_dose_to_2g"},
    contraindication_id=str(uuid.uuid4()),
)


def _flags(drug: DrugRef, rule: ContraindicationRule, panel: HepaticPanel):
    return check_contraindications(
        drug, SafetyContext(contraindication_rules=[rule], hepatic=panel)
    )


# --- the pure check ---------------------------------------------------------------------------


async def test_a_hepatic_threshold_is_evaluated_at_all() -> None:
    """The regression this file exists for: before, this returned an empty list."""
    flags = _flags(_MTX, _MTX_RULE, HepaticPanel(bilirubin_mg_dl=4.2, alt_u_l=30.0))

    assert [f.check_type for f in flags] == ["hepatic_dose"]


async def test_a_normal_liver_panel_raises_nothing() -> None:
    assert _flags(_MTX, _MTX_RULE, HepaticPanel(bilirubin_mg_dl=0.8, alt_u_l=28.0)) == []


@pytest.mark.parametrize(
    ("panel", "expected_marker"),
    [
        (HepaticPanel(bilirubin_mg_dl=4.2, alt_u_l=30.0), "total bilirubin"),
        (HepaticPanel(bilirubin_mg_dl=0.9, alt_u_l=300.0), "ALT"),
    ],
)
async def test_either_marker_alone_breaches_a_two_marker_rule(
    panel: HepaticPanel, expected_marker: str
) -> None:
    """The thresholds are alternative evidence of one impairment, not joint conditions."""
    flags = _flags(_MTX, _MTX_RULE, panel)

    assert len(flags) == 1
    assert [b["marker"] for b in flags[0].details["breached"]] == [expected_marker]


async def test_an_action_of_contraindicated_is_a_hard_block() -> None:
    flags = _flags(_MTX, _MTX_RULE, HepaticPanel(bilirubin_mg_dl=4.2))

    assert flags[0].severity == "hard_block"
    assert flags[0].is_hard_block is True
    assert flags[0].contraindication_id == _MTX_RULE.contraindication_id


async def test_a_dose_adjustment_action_is_a_warning_not_a_block() -> None:
    """Halving a paracetamol ceiling is a change of dose, not a refusal to prescribe."""
    flags = _flags(_PCM, _PCM_RULE, HepaticPanel(bilirubin_mg_dl=5.0))

    assert flags[0].severity == "warning"
    assert flags[0].is_hard_block is False
    assert "reduce_max_daily_dose_to_2g" in flags[0].summary


async def test_a_marker_the_rule_names_but_the_chart_lacks_cannot_breach() -> None:
    """A bilirubin-only chart against a two-marker rule is evaluated on the bilirubin."""
    assert _flags(_MTX, _MTX_RULE, HepaticPanel(bilirubin_mg_dl=1.0)) == []
    assert len(_flags(_MTX, _MTX_RULE, HepaticPanel(bilirubin_mg_dl=9.0))) == 1


async def test_a_value_exactly_on_the_threshold_does_not_breach() -> None:
    """Strictly above, matching the renal band's convention and the rule's own wording."""
    assert _flags(_MTX, _MTX_RULE, HepaticPanel(bilirubin_mg_dl=3.0, alt_u_l=120.0)) == []
    assert len(_flags(_MTX, _MTX_RULE, HepaticPanel(bilirubin_mg_dl=3.01))) == 1


async def test_no_liver_panel_reports_itself_unevaluated_rather_than_passing() -> None:
    """Prescribing methotrexate to a chart with no LFTs is when you want to be told that."""
    flags = _flags(_MTX, _MTX_RULE, HepaticPanel())

    assert len(flags) == 1
    flag = flags[0]
    assert flag.check_type == "hepatic_dose"
    assert flag.severity == "warning"
    assert flag.is_hard_block is False
    assert flag.details["evaluated"] is False
    assert "no liver function tests on this chart" in flag.summary
    assert flag.details["hepatic_thresholds"] == {"bilirubin_above": 3.0, "alt_above": 120}


async def test_a_rule_with_no_hepatic_threshold_is_untouched_by_this() -> None:
    """The condition-name path must still be the path for the rules written on it."""
    rule = ContraindicationRule(
        drug_reference_id="ATE-50",
        condition_name="Bronchial Asthma",
        severity="absolute",
        description="Beta-blockers in asthma",
        is_absolute=True,
    )
    ctx = SafetyContext(
        contraindication_rules=[rule],
        conditions=[PatientCondition(condition_name="Asthma")],
        hepatic=HepaticPanel(),
    )

    flags = check_contraindications(DrugRef("ATE-50", "Atenolol", "Beta-blocker"), ctx)

    assert [f.is_hard_block for f in flags] == [True]
    assert flags[0].check_type == "contraindication"


async def test_an_empty_hepatic_threshold_object_states_nothing_and_flags_nothing() -> None:
    """Curated data with `{}` or an unknown key is not a rule to report as unevaluated."""
    rule = ContraindicationRule(
        drug_reference_id="MTX-7.5",
        condition_name="Hepatic Impairment",
        severity="relative",
        description="x",
        is_absolute=False,
        hepatic_threshold={"action": "review"},
    )
    assert _flags(_MTX, rule, HepaticPanel()) == []


# --- the curated data ------------------------------------------------------------------------


async def test_the_shipped_hepatic_rules_are_evaluable() -> None:
    """Every hepatic threshold in the seed data names a marker this engine reads.

    A threshold written on a key nothing evaluates is the bug this whole file is about,
    reintroduced through the data instead of the code.
    """
    path = Path(__file__).resolve().parents[3] / "data" / "drugs" / "contraindications.json"
    rows = [r for r in json.loads(path.read_text()) if r.get("hepatic_threshold")]
    assert rows, "the seed data should exercise the hepatic axis"

    known = {"bilirubin_above", "alt_above", "action"}
    for row in rows:
        keys = set(row["hepatic_threshold"])
        assert keys <= known, f"{row['drug_reference_id']}: unknown keys {keys - known}"
        assert keys & {"bilirubin_above", "alt_above"}, (
            f"{row['drug_reference_id']}: threshold states no marker, so it can never fire"
        )
        assert "action" in keys


# --- through the service ------------------------------------------------------------------


async def _patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"hep-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Hepatic Patient",
        sex="male",
        date_of_birth=date(1966, 2, 11),
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


@pytest.mark.parametrize(
    ("marker", "value", "unit", "expected_bilirubin"),
    [
        ("Total Bilirubin", "4.2", "mg/dL", 4.2),
        ("Serum Bilirubin (Total)", "4.2", "mg%", 4.2),
        ("T. Bilirubin", "71.8", "umol/L", pytest.approx(4.198, rel=1e-3)),
    ],
)
async def test_the_panel_reads_bilirubin_however_the_lab_printed_it(
    db, marker: str, value: str, unit: str, expected_bilirubin
) -> None:
    _, patient = await _patient(db)
    await _lab(db, patient, marker, value, unit)

    panel = await SafetyService(db)._hepatic_panel(patient.id)

    assert panel.bilirubin_mg_dl == expected_bilirubin


@pytest.mark.parametrize("marker", ["ALT", "SGPT", "ALT (SGPT)", "Alanine Aminotransferase"])
async def test_the_panel_reads_alt_under_the_names_indian_reports_print(db, marker: str) -> None:
    _, patient = await _patient(db)
    await _lab(db, patient, marker, "310", "IU/L")

    panel = await SafetyService(db)._hepatic_panel(patient.id)

    assert panel.alt_u_l == 310.0


async def test_the_panel_takes_the_most_recent_draw_per_marker(db) -> None:
    """Reports arrive in whatever order the folder produced them, so date, not insertion."""
    _, patient = await _patient(db)
    await _lab(db, patient, "Total Bilirubin", "6.0", "mg/dL", when=date(2019, 1, 1))
    await _lab(db, patient, "Total Bilirubin", "1.1", "mg/dL", when=date(2026, 1, 1))

    panel = await SafetyService(db)._hepatic_panel(patient.id)

    assert panel.bilirubin_mg_dl == 1.1


async def test_an_unconvertible_liver_row_leaves_the_panel_empty_rather_than_guessing(db) -> None:
    """Which then makes the check report itself unevaluated, rather than pass."""
    _, patient = await _patient(db)
    await _lab(db, patient, "Total Bilirubin", "4.2", "furlongs")

    panel = await SafetyService(db)._hepatic_panel(patient.id)

    assert panel.bilirubin_mg_dl is None
    assert not panel


async def test_a_chart_with_no_liver_panel_is_empty_not_absent(db) -> None:
    _, patient = await _patient(db)

    assert not await SafetyService(db)._hepatic_panel(patient.id)


async def test_end_to_end_a_raised_bilirubin_blocks_methotrexate(db) -> None:
    """The whole point: seeded rule, real lab row, hard block, through the service."""
    account, patient = await _patient(db)
    await _lab(db, patient, "Total Bilirubin", "5.4", "mg/dL")

    _vocab, ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="MTX-7.5",
        drug_name=None,
    )

    assert ctx.hepatic.bilirubin_mg_dl == 5.4
    hepatic = [f for f in flags if f.check_type == "hepatic_dose"]
    assert len(hepatic) == 1
    assert hepatic[0].is_hard_block is True


async def test_end_to_end_a_chart_with_no_lfts_says_the_check_did_not_run(db) -> None:
    account, patient = await _patient(db)

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="MTX-7.5",
        drug_name=None,
    )

    hepatic = [f for f in flags if f.check_type == "hepatic_dose"]
    assert len(hepatic) == 1
    assert hepatic[0].details["evaluated"] is False
    assert hepatic[0].is_hard_block is False


async def test_the_hepatic_flag_is_persisted_as_a_check_row(db) -> None:
    from sqlalchemy import select

    from app.models.drug_safety_check import DrugSafetyCheck

    account, patient = await _patient(db)
    await _lab(db, patient, "SGPT", "410", "U/L")

    await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="MTX-7.5",
        drug_name=None,
    )

    rows = (
        (
            await db.execute(
                select(DrugSafetyCheck).where(
                    DrugSafetyCheck.patient_id == patient.id,
                    DrugSafetyCheck.check_type == "hepatic_dose",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].is_hard_block is True


async def test_a_statin_on_a_raised_alt_prompts_rather_than_blocks(db) -> None:
    """A single ALT establishes neither "persistent" nor "unexplained", which the label needs."""
    account, patient = await _patient(db)
    await _lab(db, patient, "ALT (SGPT)", "180", "U/L")

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="ATV-20",
        drug_name=None,
    )

    hepatic = [f for f in flags if f.check_type == "hepatic_dose"]
    assert len(hepatic) == 1
    assert hepatic[0].is_hard_block is False
    assert hepatic[0].severity == "warning"
