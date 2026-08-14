"""Hepatotoxic drugs stacked on one chart, which no existing check could see.

The curated ``hepatic_threshold`` rules are per-drug and written against a measured liver panel,
so they say nothing about a drug with a documented liver-injury signal and no threshold rule —
amoxicillin-clavulanate is the commonest cause of drug-induced liver injury in the published
registries and this vocabulary carried no hepatic rule for it at all. And the interaction table
is pairwise on curated pairs, so three separately unremarkable hepatotoxic drugs on one chart
produced three empty checks: the burden is a property of the *set*, which is the same reason
``check_duplicate_therapy`` had to exist alongside the interaction rules.

The load-bearing negative in this file is that an uncurated drug contributes nothing and is never
counted as clean. Fifty seeded drugs is not a pharmacopoeia.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.safety import (
    DrugRef,
    HepaticPanel,
    SafetyContext,
    check_hepatotoxic_burden,
    evaluate_drug_safety,
)
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

pytestmark = pytest.mark.asyncio

_AUGMENTIN = DrugRef(
    "AMX-CLV-625", "Amoxicillin + Clavulanic acid", "Penicillin + BLI", "established"
)
_STATIN = DrugRef("ATV-20", "Atorvastatin", "Statin", "established")
_PARACETAMOL = DrugRef("PCM-650", "Paracetamol", "Analgesic", "dose_dependent")
# Curated, and curated as carrying no liver-injury tier.
_AMLODIPINE = DrugRef("AML-5", "Amlodipine", "CCB")
# In the vocabulary but with nothing said about its liver risk either way.
_UNCURATED = DrugRef("XYZ-1", "Somethingelse", "Unknown")

_DECOMPENSATED = HepaticPanel(bilirubin_mg_dl=8.0, albumin_g_dl=2.1, inr=3.4)
_MILDLY_ABNORMAL = HepaticPanel(bilirubin_mg_dl=2.5, albumin_g_dl=3.0, inr=1.8)
_HEALTHY = HepaticPanel(bilirubin_mg_dl=0.8, albumin_g_dl=4.2, inr=1.0)


def _flags(proposed: DrugRef, *current: DrugRef, panel: HepaticPanel | None = None):
    return check_hepatotoxic_burden(
        proposed,
        SafetyContext(current_meds=list(current), hepatic=panel or HepaticPanel()),
    )


# --- when it fires --------------------------------------------------------------------------


async def test_a_second_hepatotoxic_drug_on_a_chart_is_flagged() -> None:
    """No interaction rule covers this pair and none should have to: the burden is the set."""
    flags = _flags(_AUGMENTIN, _STATIN)

    assert len(flags) == 1
    assert flags[0].check_type == "hepatotoxic_burden"
    assert flags[0].details["concurrent_hepatotoxic_drugs"] == ["Atorvastatin"]


async def test_every_concurrent_hepatotoxic_drug_is_named_not_just_counted() -> None:
    flags = _flags(_AUGMENTIN, _STATIN, _PARACETAMOL, _AMLODIPINE)

    assert flags[0].details["concurrent_hepatotoxic_drugs"] == ["Atorvastatin", "Paracetamol"]
    assert "Amlodipine" not in flags[0].summary


async def test_a_hepatotoxic_drug_against_an_impaired_liver_is_flagged_alone() -> None:
    """No second drug needed. This is the case the per-drug threshold rules cannot reach for a
    drug they hold no rule for, which is most of the vocabulary."""
    flags = _flags(_AUGMENTIN, panel=_DECOMPENSATED)

    assert len(flags) == 1
    assert flags[0].details["hepatic_impairment"] is True
    assert flags[0].details["concurrent_hepatotoxic_drugs"] == []


async def test_a_determinate_class_c_raises_the_flag_to_critical() -> None:
    """Conservative wins on disagreement — CLAUDE.md safety rule #2."""
    assert _flags(_AUGMENTIN, panel=_DECOMPENSATED)[0].severity == "critical"
    assert _flags(_AUGMENTIN, panel=_MILDLY_ABNORMAL)[0].severity == "warning"
    assert _flags(_AUGMENTIN, _STATIN)[0].severity == "warning"


async def test_two_reasons_produce_one_flag_stating_both() -> None:
    flags = _flags(_AUGMENTIN, _STATIN, panel=_DECOMPENSATED)

    assert len(flags) == 1
    summary = flags[0].summary
    assert "Atorvastatin" in summary
    assert "Child-Pugh C" in summary


# --- when it stays quiet --------------------------------------------------------------------


async def test_a_lone_hepatotoxic_drug_on_a_healthy_liver_is_not_flagged() -> None:
    """Prescribing one hepatotoxic drug is ordinary practice. Flagging it would be an
    unclearable alert on a large fraction of every antibiotic course in the country."""
    assert _flags(_AUGMENTIN, panel=_HEALTHY) == []
    assert _flags(_AUGMENTIN) == []


async def test_a_non_hepatotoxic_proposal_is_never_flagged_by_the_chart_around_it() -> None:
    """A chart full of hepatotoxic drugs is not a reason to flag an antihypertensive."""
    assert _flags(_AMLODIPINE, _AUGMENTIN, _STATIN, panel=_DECOMPENSATED) == []


async def test_reordering_the_same_drug_is_not_a_second_hepatotoxic_drug() -> None:
    """The proposed drug stays in current_meds so duplicate-therapy can see it; counting it
    against itself would flag every repeat prescription of a hepatotoxic drug."""
    assert _flags(_AUGMENTIN, _AUGMENTIN) == []


async def test_child_pugh_class_a_at_its_floor_is_not_impairment() -> None:
    """A healthy panel bounds to A-B. Treating the *worst case* the labs permit as impairment
    would fire on every chart that happens to carry a full liver panel."""
    assert _flags(_AUGMENTIN, panel=_HEALTHY) == []


# --- the load-bearing negative ---------------------------------------------------------------


async def test_an_uncurated_drug_is_not_counted_as_a_clean_one() -> None:
    """Silence about a drug's liver risk is not a statement that it has none. A check that read
    "no other hepatotoxic drugs on this chart" off a fifty-drug table would be asserting
    something the table cannot support."""
    assert _flags(_UNCURATED, _AUGMENTIN, _STATIN, panel=_DECOMPENSATED) == []
    # And an uncurated *current* medication adds nothing to the count either way.
    assert _flags(_AUGMENTIN, _UNCURATED) == []


async def test_the_summary_says_the_count_is_only_over_curated_drugs() -> None:
    """Because the count is a floor, not a total, and a clinician reading "1 other medication
    with a liver-injury signal" would otherwise take it for the whole chart."""
    summary = _flags(_AUGMENTIN, _STATIN)[0].summary

    assert "only against the drugs this vocabulary curates" in summary


async def test_an_unrecognised_tier_string_is_treated_as_uncurated() -> None:
    """Seed data with a typo or a tier from a future revision must not silently become a flag
    with a blank explanation, nor silently become a clean drug."""
    typo = DrugRef("AMX-CLV-625", "Amoxicillin + Clavulanic acid", None, "hepatotixic")

    assert _flags(typo, _STATIN, panel=_DECOMPENSATED) == []
    assert _flags(_AUGMENTIN, typo) == []


# --- what it never does ----------------------------------------------------------------------


async def test_it_is_never_a_hard_block() -> None:
    """A diabetic on a statin who needs a course of co-amoxiclav is not a prescribing error.
    The decision needs the clinician, not a refusal."""
    for flags in (
        _flags(_AUGMENTIN, _STATIN),
        _flags(_AUGMENTIN, panel=_DECOMPENSATED),
        _flags(_PARACETAMOL, _AUGMENTIN, _STATIN, panel=_DECOMPENSATED),
    ):
        assert flags[0].is_hard_block is False
        assert flags[0].severity != "hard_block"


async def test_the_summary_is_prescriber_framed() -> None:
    """CLAUDE.md safety rule #4, checked on generated text rather than on the template."""
    summary = _flags(_AUGMENTIN, _STATIN, panel=_DECOMPENSATED)[0].summary
    assert "Guidelines support" in summary
    for banned in ("Give ", "Administer ", "Stop the ", "The patient has ", "Do not prescribe"):
        assert banned not in summary


async def test_it_runs_inside_the_whole_engine_not_only_on_its_own() -> None:
    """A check nothing calls is the failure this file's neighbours were written for."""
    flags = evaluate_drug_safety(
        _AUGMENTIN, SafetyContext(current_meds=[_STATIN], hepatic=_DECOMPENSATED)
    )

    assert "hepatotoxic_burden" in {f.check_type for f in flags}


# --- the curated data -------------------------------------------------------------------------


async def test_every_seeded_tier_is_one_the_engine_evaluates() -> None:
    """A tier written on a value nothing reads would load, match nothing and fall out silently —
    the shape of the bug the hepatic axis itself was added to end."""
    root = Path(__file__).resolve().parents[3]
    rows = json.loads((root / "data" / "drugs" / "drug_vocabulary.json").read_text())

    tiers = {row["hepatotoxicity"] for row in rows if row.get("hepatotoxicity")}

    assert tiers, "no drug in the seed vocabulary carries a liver-injury tier"
    assert tiers <= {"dose_dependent", "established"}, tiers
    for tier in tiers:
        assert _flags(DrugRef("X", "X", None, tier), _STATIN), f"{tier} evaluates to nothing"


async def test_the_commonest_cause_of_dili_is_actually_tagged() -> None:
    """The gap that motivated the column. Amoxicillin-clavulanate carries no hepatic threshold
    rule and had nothing else saying anything about its liver risk."""
    root = Path(__file__).resolve().parents[3]
    rows = json.loads((root / "data" / "drugs" / "drug_vocabulary.json").read_text())
    by_ref = {row["reference_id"]: row for row in rows}

    assert by_ref["AMX-CLV-625"].get("hepatotoxicity") == "established"
    assert by_ref["PCM-650"].get("hepatotoxicity") == "dose_dependent"
    # And a drug with no liver signal stays untagged rather than being given a reassuring one.
    assert "hepatotoxicity" not in by_ref["AML-5"]


# --- through the service ------------------------------------------------------------------


async def _patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"hepburden-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Burden Patient",
        sex="female",
        date_of_birth=date(1974, 9, 2),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _medication(db, patient, name: str) -> None:
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            generic_name=name,
            event_type="start",
            is_current=True,
            event_date=date(2026, 2, 1),
        )
    )
    await db.flush()


async def _lab(db, patient, marker: str, value: str, unit: str) -> None:
    db.add(
        LabResult(
            patient_id=patient.id,
            marker_name=marker,
            value_numeric=Decimal(value),
            unit=unit,
            sample_date=date(2026, 3, 1),
        )
    )
    await db.flush()


async def test_end_to_end_a_statin_plus_co_amoxiclav_reaches_the_clinician(db) -> None:
    account, patient = await _patient(db)
    await _medication(db, patient, "Atorvastatin")

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="AMX-CLV-625",
        drug_name=None,
    )

    burden = [f for f in flags if f.check_type == "hepatotoxic_burden"]
    assert len(burden) == 1
    assert burden[0].details["concurrent_hepatotoxic_drugs"] == ["Atorvastatin"]
    assert burden[0].is_hard_block is False


async def test_end_to_end_the_tier_survives_a_medication_linked_only_by_name(db) -> None:
    """A row imported before the vocabulary knew the brand resolves by name. If the tier did not
    ride along that path, a chart's hepatotoxic burden would depend on which of its rows happened
    to have a vocabulary id."""
    account, patient = await _patient(db)
    await _medication(db, patient, "Atorva")  # brand name, no drug_vocabulary_id

    _vocab, ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="AMX-CLV-625",
        drug_name=None,
    )

    assert [m.hepatotoxicity for m in ctx.current_meds] == ["established"]
    assert [f.check_type for f in flags if f.check_type == "hepatotoxic_burden"] == [
        "hepatotoxic_burden"
    ]


async def test_end_to_end_the_flag_is_persisted_as_a_check_row(db) -> None:
    from sqlalchemy import select

    from app.models.drug_safety_check import DrugSafetyCheck

    account, patient = await _patient(db)
    await _lab(db, patient, "Total Bilirubin", "8.0", "mg/dL")
    await _lab(db, patient, "Serum Albumin", "2.1", "g/dL")
    await _lab(db, patient, "INR", "3.4", "")

    await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="AMX-CLV-625",
        drug_name=None,
    )

    rows = (
        (
            await db.execute(
                select(DrugSafetyCheck).where(
                    DrugSafetyCheck.patient_id == patient.id,
                    DrugSafetyCheck.check_type == "hepatotoxic_burden",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].severity == "critical"
    assert rows[0].is_hard_block is False
