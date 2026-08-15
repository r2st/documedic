"""An absolute contraindication must survive the chart wording the condition differently.

``check_contraindications`` compared the curated rule's ``condition_name`` to the chart's with
string equality, so the hard block CLAUDE.md rule 3 calls non-negotiable was enforced only when a
clinician happened to spell the condition exactly as the seed data does. A chart saying "Asthma"
— the ordinary way it is written — did not block atenolol; "Bronchial Asthma" did. The failure
was silent and open: ``is_blocked: false``, no flag, nothing recording that the comparison had
been attempted and missed.

Matching now runs on three deterministic, offline axes (rule 8): ICD-10 code, synonym-rewritten
word sets, and specificity. The asymmetry is the point — a chart MORE specific than the rule
("Ectopic Pregnancy" for a rule on "Pregnancy") still blocks, a chart LESS specific ("Renal
Impairment" for a rule on *Severe* renal impairment) becomes a warning, and a chart that hedges
("suspected", "h/o") becomes a warning. Nothing related is dropped in silence.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.safety import (
    ContraindicationRule,
    DrugRef,
    PatientCondition,
    SafetyContext,
    check_contraindications,
    evaluate_drug_safety,
    has_hard_block,
)
from app.models.condition import Condition
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

ATENOLOL = DrugRef("ATE-50", "Atenolol", "Beta Blocker")
WARFARIN = DrugRef("WARF-5", "Warfarin", "Anticoagulant")
DICLOFENAC = DrugRef("DIC-50", "Diclofenac", "NSAID")
LITHIUM = DrugRef("LIT-400", "Lithium", "Mood Stabiliser")

# The seeded absolute contraindication this whole module is written around: atenolol in asthma.
ASTHMA_RULE = ContraindicationRule(
    drug_reference_id="ATE-50",
    condition_name="Bronchial Asthma",
    severity="absolute",
    description="Beta blockade can precipitate bronchospasm.",
    is_absolute=True,
    contraindication_id="ci-asthma",
)
PREGNANCY_RULE = ContraindicationRule(
    drug_reference_id="WARF-5",
    condition_name="Pregnancy",
    severity="absolute",
    description="Teratogenic; crosses the placenta.",
    is_absolute=True,
    contraindication_id="ci-pregnancy",
)
PUD_RULE = ContraindicationRule(
    drug_reference_id="DIC-50",
    condition_name="Peptic Ulcer Disease",
    severity="absolute",
    description="Risk of gastrointestinal haemorrhage.",
    is_absolute=True,
    contraindication_id="ci-pud",
)
# A non-absolute rule, to pin that the same matching drives the advisory branch too.
SEVERE_RENAL_RULE = ContraindicationRule(
    drug_reference_id="LIT-400",
    condition_name="Severe Renal Impairment",
    severity="dose_adjustment_required",
    description="Reduced lithium clearance.",
    is_absolute=False,
    contraindication_id="ci-renal",
)


def _check(drug: DrugRef, rule: ContraindicationRule, charted: str, icd10: str | None = None):
    ctx = SafetyContext(
        conditions=[PatientCondition(condition_name=charted, icd10_code=icd10)],
        contraindication_rules=[rule],
    )
    return check_contraindications(drug, ctx)


# --- the bug: wording variance defeated the hard block ----------------------------------------


@pytest.mark.parametrize(
    "charted",
    [
        "Bronchial Asthma",  # verbatim — the only wording that used to work
        "Asthma",  # the ordinary way it is written, and the bug
        "asthma",
        "ASTHMA",
        "Bronchial asthma",
        "Asthma (moderate persistent)",  # parenthetical qualifier
        "Asthma [J45]",
        "Severe Persistent Asthma",  # the rule's condition plus detail
        "Reactive Airway Disease",  # clinical synonym
        "Bronchospasm",
    ],
)
def test_asthma_written_any_of_these_ways_still_hard_blocks_atenolol(charted: str) -> None:
    """The regression this module exists for. Every one of these is asthma on the chart.

    A beta blocker in an asthmatic can precipitate fatal bronchospasm; whether the block fires
    must not depend on which of these a clinician typed.
    """
    flags = _check(ATENOLOL, ASTHMA_RULE, charted)

    assert len(flags) == 1, f"{charted!r} produced {[f.summary for f in flags]}"
    assert flags[0].is_hard_block is True
    assert flags[0].severity == "hard_block"
    assert flags[0].check_type == "contraindication"


@pytest.mark.parametrize(
    ("drug", "rule", "charted"),
    [
        (WARFARIN, PREGNANCY_RULE, "Pregnancy"),
        (WARFARIN, PREGNANCY_RULE, "Pregnant"),
        (WARFARIN, PREGNANCY_RULE, "Primigravida"),
        (WARFARIN, PREGNANCY_RULE, "Ectopic Pregnancy"),
        (WARFARIN, PREGNANCY_RULE, "Pregnancy - 12 weeks"),
        (DICLOFENAC, PUD_RULE, "Peptic Ulcer Disease"),
        (DICLOFENAC, PUD_RULE, "Peptic ulcer"),
        (DICLOFENAC, PUD_RULE, "PUD"),
        (DICLOFENAC, PUD_RULE, "Duodenal Ulcer"),
        (DICLOFENAC, PUD_RULE, "Gastric ulcer"),
    ],
)
def test_the_other_seeded_absolute_rules_survive_their_wordings(drug, rule, charted) -> None:
    """The same widening across the rest of the seeded absolute contraindications."""
    flags = _check(drug, rule, charted)

    assert len(flags) == 1
    assert flags[0].is_hard_block is True


def test_british_spelling_on_the_chart_still_matches_the_rule() -> None:
    """Indian charts are written in British English; the curated rules are not."""
    rule = ContraindicationRule(
        drug_reference_id="WARF-5",
        condition_name="Active Hemorrhage",
        severity="absolute",
        description="Bleeding risk.",
        is_absolute=True,
        contraindication_id="ci-bleed",
    )

    assert _check(WARFARIN, rule, "Active haemorrhage")[0].is_hard_block is True


def test_a_block_records_what_the_chart_actually_said() -> None:
    """The chart's wording and the reason it was accepted, on the record.

    A hard block is exactly the decision that has to be answerable months later, and once the
    rule's own wording is no longer necessarily the chart's, the flag has to carry both.
    """
    flags = _check(ATENOLOL, ASTHMA_RULE, "Severe Persistent Asthma")

    assert flags[0].details["condition"] == "Bronchial Asthma"
    assert flags[0].details["charted_condition"] == "Severe Persistent Asthma"
    assert flags[0].details["match_basis"] == "more_specific"
    assert flags[0].details["is_absolute"] is True


# --- what must NOT start blocking -------------------------------------------------------------


@pytest.mark.parametrize(
    "charted",
    [
        "No h/o asthma",
        "No asthma",
        "Not asthma",
        "Asthma - ruled out",
        "Family history of asthma",
        "Negative for asthma",
        "Nil asthma",
        "Patient denies asthma",
    ],
)
def test_a_condition_the_chart_says_the_patient_does_not_have_raises_nothing(charted) -> None:
    """Widening the comparison must not turn a documented absence into a block.

    "Not pregnant" blocking warfarin would be the widening doing exactly the damage it was
    written to prevent, in the other direction.
    """
    assert _check(ATENOLOL, ASTHMA_RULE, charted) == []


def test_not_pregnant_does_not_block_warfarin() -> None:
    """The negation case that matters most, spelled out."""
    assert _check(WARFARIN, PREGNANCY_RULE, "Not pregnant") == []


@pytest.mark.parametrize(
    "charted", ["Hypertension", "Type 2 Diabetes Mellitus", "Migraine", "", "   "]
)
def test_an_unrelated_or_empty_condition_raises_nothing(charted: str) -> None:
    """Nothing here may fire on a condition that has no relation to the rule."""
    assert _check(ATENOLOL, ASTHMA_RULE, charted) == []


def test_a_rule_for_a_different_drug_is_never_consulted() -> None:
    """Unchanged, and worth keeping pinned: rules are scoped to the proposed drug."""
    ctx = SafetyContext(
        conditions=[PatientCondition("Bronchial Asthma")], contraindication_rules=[ASTHMA_RULE]
    )

    assert check_contraindications(WARFARIN, ctx) == []


def test_two_renal_bands_do_not_collapse_into_each_other() -> None:
    """ "Moderate" and "Severe" renal impairment are two curated rules with two eGFR bands.

    Severity words are deliberately not treated as noise-words, because answering the moderate
    band with the severe band's rule would be answering with the wrong threshold.
    """
    assert _check(LITHIUM, SEVERE_RENAL_RULE, "Moderate Renal Impairment") == []


# --- the residue: related, but not established --------------------------------------------------


@pytest.mark.parametrize("charted", ["h/o asthma", "History of asthma", "Suspected asthma"])
def test_a_hedged_or_historical_condition_warns_instead_of_blocking(charted: str) -> None:
    """The chart does not assert the patient currently has it, so the block is not applied.

    Saying nothing would be the original bug wearing a different hat: this is the one condition
    on the chart that bears on this drug, and only the clinician can close the gap.
    """
    flags = _check(ATENOLOL, ASTHMA_RULE, charted)

    assert len(flags) == 1
    flag = flags[0]
    assert flag.is_hard_block is False
    assert flag.severity == "warning"
    assert flag.details["would_hard_block_if_confirmed"] is True
    assert flag.details["evaluated"] is False
    assert flag.details["charted_condition"] == charted
    assert charted in flag.summary
    assert "does not establish it" in flag.summary


@pytest.mark.parametrize(
    ("rule", "drug", "charted"),
    [
        (SEVERE_RENAL_RULE, LITHIUM, "Renal Impairment"),
        (PUD_RULE, DICLOFENAC, "Ulcer"),
    ],
)
def test_a_chart_less_specific_than_the_rule_warns_instead_of_blocking(rule, drug, charted):
    """ "Renal Impairment" is not evidence the patient meets a rule written for *severe* renal
    impairment, so asserting the rule would be asserting something the record does not say."""
    flags = check_contraindications(
        drug,
        SafetyContext(conditions=[PatientCondition(charted)], contraindication_rules=[rule]),
    )

    assert len(flags) == 1
    assert flags[0].is_hard_block is False
    assert flags[0].details["match_basis"] == "less_specific"


def test_a_near_miss_on_an_advisory_rule_is_informational_not_a_warning() -> None:
    """Severity tracks what the underlying rule would have been, so an advisory rule's
    unresolved near-miss does not shout as loudly as an absolute one's."""
    flags = _check(LITHIUM, SEVERE_RENAL_RULE, "Renal Impairment")

    assert flags[0].severity == "info"
    assert flags[0].details["would_hard_block_if_confirmed"] is False
    assert flags[0].details["rule_severity"] == "dose_adjustment_required"


def test_an_established_diagnosis_beats_a_hedged_one_on_the_same_chart() -> None:
    """A chart carrying both "h/o asthma" and "Asthma" describes an asthmatic. It blocks."""
    ctx = SafetyContext(
        conditions=[PatientCondition("h/o asthma"), PatientCondition("Asthma")],
        contraindication_rules=[ASTHMA_RULE],
    )

    flags = check_contraindications(ATENOLOL, ctx)

    assert len(flags) == 1, "one flag per rule, not one per charted condition"
    assert flags[0].is_hard_block is True


def test_a_near_miss_never_becomes_a_hard_block_through_evaluate_drug_safety() -> None:
    """The aggregate verdict, which is what the endpoint reports as ``is_blocked``."""
    ctx = SafetyContext(
        conditions=[PatientCondition("Suspected asthma")], contraindication_rules=[ASTHMA_RULE]
    )

    assert has_hard_block(evaluate_drug_safety(ATENOLOL, ctx)) is False


# --- the ICD-10 axis ----------------------------------------------------------------------------


def test_a_matching_icd10_code_blocks_however_the_condition_is_worded() -> None:
    """A code equality is unambiguous where a name comparison is a guess about wording.

    "Wheezy bronchitis" shares no word with "Bronchial Asthma"; coded J45 it is the same
    condition, and the block has to fire.
    """
    coded_rule = ContraindicationRule(
        drug_reference_id="ATE-50",
        condition_name="Bronchial Asthma",
        severity="absolute",
        description="Beta blockade can precipitate bronchospasm.",
        is_absolute=True,
        contraindication_id="ci-asthma",
        icd10_code="J45",
    )

    flags = _check(ATENOLOL, coded_rule, "Wheezy bronchitis", icd10="J45.909")

    assert len(flags) == 1
    assert flags[0].is_hard_block is True
    assert flags[0].details["match_basis"] == "icd10"


@pytest.mark.parametrize(
    ("rule_code", "chart_code"),
    [
        ("J45", "I10"),  # different conditions
        ("J45", None),  # chart uncoded
        (None, "J45.909"),  # rule uncoded — the seeded state today
        ("J4", "J45"),  # below the three-character floor: never a whole-chapter match
        ("J45", ""),
    ],
)
def test_codes_that_do_not_identify_the_same_condition_do_not_match(rule_code, chart_code) -> None:
    """The code axis must not manufacture a match out of a partial or absent code."""
    rule = ContraindicationRule(
        drug_reference_id="ATE-50",
        condition_name="Bronchial Asthma",
        severity="absolute",
        description="Bronchospasm.",
        is_absolute=True,
        contraindication_id="ci-asthma",
        icd10_code=rule_code,
    )

    assert _check(ATENOLOL, rule, "Wheezy bronchitis", icd10=chart_code) == []


# --- unchanged behaviour that shares the code path ---------------------------------------------


def test_a_renal_threshold_rule_is_still_decided_by_egfr_not_by_the_condition_name() -> None:
    """Renal rules are evaluated against a measured value and return before name matching.

    Pinned because the name-matching rewrite sits directly beneath that branch.
    """
    rule = ContraindicationRule(
        drug_reference_id="MET-500",
        condition_name="Chronic Kidney Disease",
        severity="dose_adjustment_required",
        description="Reduced clearance.",
        is_absolute=False,
        renal_threshold={"egfr_below": 30, "action": "contraindicated"},
        contraindication_id="ci-met",
    )
    metformin = DrugRef("MET-500", "Metformin", "Biguanide")
    ctx = SafetyContext(egfr=22.0, conditions=[], contraindication_rules=[rule])

    flags = check_contraindications(metformin, ctx)

    assert len(flags) == 1
    assert flags[0].check_type == "renal_dose"
    assert flags[0].is_hard_block is True


# --- through the service and the API -----------------------------------------------------------


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"ci-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Contraindication Patient",
        sex="female",
        date_of_birth=datetime(1990, 4, 11).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def test_the_seeded_atenolol_block_fires_for_a_chart_that_says_asthma(db) -> None:
    """End to end against the real seeded contraindication table.

    This is the bug as a clinician would have met it: an asthmatic patient, atenolol proposed,
    and a verdict of "not blocked".
    """
    account, patient = await _account_and_patient(db)
    db.add(Condition(patient_id=patient.id, condition_name="Asthma", status="active"))
    await db.flush()

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Atenolol",
    )

    blocks = [f for f in flags if f.check_type == "contraindication" and f.is_hard_block]
    assert len(blocks) == 1
    assert blocks[0].details["charted_condition"] == "Asthma"
    assert has_hard_block(flags) is True


async def test_a_resolved_condition_is_still_not_consulted(db) -> None:
    """A condition the chart says the patient no longer has raises nothing.

    Load-bearing for the above, and the one half of the status filter that survived the
    widening in ``test_condition_status_scoping`` — "resolved" and "inactive" are the only two
    statuses that still keep a row out of the context.
    """
    account, patient = await _account_and_patient(db)
    db.add(Condition(patient_id=patient.id, condition_name="Asthma", status="resolved"))
    await db.flush()

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Atenolol",
    )

    assert has_hard_block(flags) is False


async def test_the_endpoint_reports_the_block_for_a_chart_that_says_asthma(auth_client, db) -> None:
    """``is_blocked`` is what the Safety screen renders, and it said false."""
    from tests.conftest import create_patient

    patient = await create_patient(auth_client)
    db.add(Condition(patient_id=uuid.UUID(patient["id"]), condition_name="Asthma", status="active"))
    await db.commit()

    response = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Atenolol"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_blocked"] is True
    blocks = [f for f in body["flags"] if f["is_hard_block"]]
    assert len(blocks) == 1
    assert blocks[0]["check_type"] == "contraindication"
    assert blocks[0]["details"]["charted_condition"] == "Asthma"
