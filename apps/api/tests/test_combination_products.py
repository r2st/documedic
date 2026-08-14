"""A fixed-dose combination was invisible to every rule in the deterministic engine.

Each curated rule here is keyed on a single molecule: an interaction on a pair of reference ids,
a contraindication on a reference id and a condition, an allergy match on a generic name or a
drug class. A combination product is one vocabulary row carrying several molecules, and matching
it against those rules by its own reference id, its own generic name and its own drug class
matched it against nothing.

Nothing is what came back. Glycomet GP (metformin + glimepiride) prescribed at an eGFR of 20
returned no flags at all — the metformin hard block below 30 is written on MET-500, and
MET-GLM-1-500 is not MET-500. Augmentin returned no flags for a patient with a documented
amoxicillin allergy, because "Amoxicillin + Clavulanic acid" is not "Amoxicillin" and the class
"Penicillin + BLI" is not "Penicillin": the commonest drug allergy in this product's market,
against the combination form of the drug it is an allergy to, failing open. Telma 40 alongside
Telma H is a doubled telmisartan dose that was reported as two unrelated drugs.

That is CLAUDE.md rule 3 defeated by formulation, on the products that are among the most
prescribed in India. The load-bearing tests here are the ones that assert a *block*, and the two
negatives that keep the fix from over-firing: an ingredient with no reference id of its own must
not be confused with a different one, and the ingredients inside one licensed product must not be
flagged as interacting with each other.
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
    InteractionRule,
    PatientAllergy,
    PatientCondition,
    SafetyContext,
    check_allergies,
    check_contraindications,
    check_duplicate_therapy,
    check_guideline_adherence,
    check_interactions,
    evaluate_drug_safety,
    has_hard_block,
    ingredient_reference_ids,
)
from app.models.allergy import Allergy
from app.models.derived_marker import DerivedMarker
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService, _components, _drug_ref

pytestmark = pytest.mark.asyncio

_VOCABULARY = Path(__file__).resolve().parents[3] / "data" / "drugs" / "drug_vocabulary.json"

# The products as the seed vocabulary now describes them.
_GLYCOMET_GP = DrugRef(
    "MET-GLM-1-500",
    "Metformin + Glimepiride",
    "Biguanide + Sulfonylurea",
    components=(
        DrugRef("MET-500", "Metformin", "Biguanide"),
        DrugRef("GLM-1", "Glimepiride", "Sulfonylurea"),
    ),
)
_TELMA_H = DrugRef(
    "TEL-HCTZ-40",
    "Telmisartan + HCTZ",
    "ARB + Thiazide",
    components=(
        DrugRef("TEL-40", "Telmisartan", "ARB"),
        DrugRef("", "Hydrochlorothiazide", "Thiazide diuretic"),
    ),
)
_AUGMENTIN = DrugRef(
    "AMX-CLV-625",
    "Amoxicillin + Clavulanic acid",
    "Penicillin + BLI",
    "established",
    components=(
        DrugRef("", "Amoxicillin", "Penicillin"),
        DrugRef("", "Clavulanic acid", "Beta-lactamase inhibitor"),
    ),
)
_SEPTRAN = DrugRef(
    "TMP-SMX-960",
    "Cotrimoxazole",
    "Sulfonamide antibiotic",
    "established",
    components=(
        DrugRef("", "Trimethoprim", "Antifolate"),
        DrugRef("", "Sulfamethoxazole", "Sulfonamide antibiotic"),
    ),
)

_METFORMIN = DrugRef("MET-500", "Metformin", "Biguanide")
_TELMISARTAN = DrugRef("TEL-40", "Telmisartan", "ARB")
_ENALAPRIL = DrugRef("ENA-5", "Enalapril", "ACE Inhibitor")
_CONTRAST = DrugRef("CONTRAST-IODINE", "Iodinated contrast media", "Radiocontrast agent")
_AMLODIPINE = DrugRef("AML-5", "Amlodipine", "CCB")

_METFORMIN_CKD = ContraindicationRule(
    drug_reference_id="MET-500",
    condition_name="Chronic Kidney Disease",
    severity="dose_adjustment_required",
    description="Risk of lactic acidosis.",
    is_absolute=False,
    renal_threshold={"egfr_below": 30, "action": "contraindicated"},
)
_METFORMIN_ABSOLUTE = ContraindicationRule(
    drug_reference_id="MET-500",
    condition_name="Diabetic Ketoacidosis",
    severity="absolute",
    description="Metformin is contraindicated in ketoacidosis.",
    is_absolute=True,
)
_CONTRAST_METFORMIN = InteractionRule(
    drug_a_reference_id="CONTRAST-IODINE",
    drug_b_reference_id="MET-500",
    severity="major",
    description="Increased risk of lactic acidosis.",
    management="Withhold metformin 48h before and after contrast.",
)
_ACEI_ARB = InteractionRule(
    drug_a_reference_id="ENA-5",
    drug_b_reference_id="TEL-40",
    severity="major",
    description="Dual RAAS blockade: hyperkalaemia and acute kidney injury.",
)


# --- contraindications reached through an ingredient -------------------------------------------


async def test_a_combination_is_hard_blocked_by_its_ingredient_renal_rule() -> None:
    """The motivating failure. Glycomet GP at eGFR 20 returned nothing at all."""
    flags = check_contraindications(
        _GLYCOMET_GP, SafetyContext(egfr=20.0, contraindication_rules=[_METFORMIN_CKD])
    )

    assert len(flags) == 1
    assert flags[0].is_hard_block is True
    assert flags[0].check_type == "renal_dose"
    assert flags[0].details["component"] == "Metformin"
    assert flags[0].details["proposed_drug"] == "Metformin + Glimepiride"


async def test_the_flag_names_the_product_the_clinician_prescribed() -> None:
    """A warning about "Metformin" on a chart whose prescription says Glycomet GP reads as being
    about some other drug."""
    flag = check_contraindications(
        _GLYCOMET_GP, SafetyContext(egfr=20.0, contraindication_rules=[_METFORMIN_CKD])
    )[0]

    assert "Metformin + Glimepiride" in flag.summary
    assert "via its Metformin component" in flag.summary


async def test_an_absolute_contraindication_on_an_ingredient_still_hard_blocks() -> None:
    ctx = SafetyContext(
        conditions=[PatientCondition(condition_name="Diabetic Ketoacidosis")],
        contraindication_rules=[_METFORMIN_ABSOLUTE],
    )

    flags = check_contraindications(_GLYCOMET_GP, ctx)

    assert [f.is_hard_block for f in flags] == [True]
    assert flags[0].details["component"] == "Metformin"


async def test_a_missing_egfr_is_reported_against_the_combination_too() -> None:
    """ "Could not be checked" must not become "checked and fine" for a combination either."""
    flags = check_contraindications(
        _GLYCOMET_GP, SafetyContext(egfr=None, contraindication_rules=[_METFORMIN_CKD])
    )

    assert len(flags) == 1
    assert flags[0].details["evaluated"] is False
    assert flags[0].is_hard_block is False


async def test_an_ingredient_above_the_threshold_is_not_flagged() -> None:
    assert (
        check_contraindications(
            _GLYCOMET_GP, SafetyContext(egfr=80.0, contraindication_rules=[_METFORMIN_CKD])
        )
        == []
    )


# --- allergies reached through an ingredient ---------------------------------------------------


async def test_an_allergy_to_an_ingredient_hard_blocks_the_combination() -> None:
    """Amoxicillin allergy against Augmentin. Clavulanic acid does not make it a different drug."""
    ctx = SafetyContext(allergies=[PatientAllergy(allergen_name="Amoxicillin", allergy_id="a1")])

    flags = check_allergies(_AUGMENTIN, ctx)

    assert len(flags) == 1
    assert flags[0].is_hard_block is True
    assert flags[0].details["match_type"] == "direct"
    assert flags[0].details["component"] == "Amoxicillin"


async def test_an_allergy_matched_by_ingredient_class_hard_blocks() -> None:
    ctx = SafetyContext(
        allergies=[
            PatientAllergy(allergen_name="Ampicillin", drug_class="Penicillin", allergy_id="a1")
        ]
    )

    flags = check_allergies(_AUGMENTIN, ctx)

    assert [f.details["match_type"] for f in flags] == ["cross_class"]
    assert flags[0].is_hard_block is True


async def test_class_cross_reactivity_reaches_an_ingredient_class() -> None:
    """A cephalosporin allergy cross-reacts with penicillins. Before ingredients, the only
    penicillin in this vocabulary carried the class "Penicillin + BLI" and matched no family."""
    ctx = SafetyContext(
        allergies=[
            PatientAllergy(allergen_name="Ceftriaxone", drug_class="Cephalosporin", allergy_id="a")
        ]
    )

    flags = check_allergies(_AUGMENTIN, ctx)

    assert [f.details["match_type"] for f in flags] == ["cross_reactivity"]
    assert flags[0].severity == "critical"
    assert flags[0].is_hard_block is False
    assert flags[0].details["component"] == "Amoxicillin"


async def test_one_allergy_produces_one_flag_even_across_several_ingredients() -> None:
    """A sulfa-antibiotic allergy both matches Septran's own class and its sulfamethoxazole
    component's. Two flags for one conflict is noise on a card that cannot be cleared anyway."""
    ctx = SafetyContext(
        allergies=[
            PatientAllergy(
                allergen_name="Sulfamethoxazole",
                drug_class="Sulfonamide antibiotic",
                allergy_id="a1",
            )
        ]
    )

    flags = check_allergies(_SEPTRAN, ctx)

    assert len(flags) == 1
    assert flags[0].is_hard_block is True


async def test_a_hard_block_wins_over_a_cross_reactivity_flag_for_one_allergen() -> None:
    """Glycomet GP against a sulfonylurea allergy: glimepiride is a direct class match (hard
    block) and metformin is not, while the sulfa family would also cross-react. The
    undismissible answer is the one that must reach the clinician."""
    ctx = SafetyContext(
        allergies=[
            PatientAllergy(allergen_name="Gliclazide", drug_class="Sulfonylurea", allergy_id="a1")
        ]
    )

    flags = check_allergies(_GLYCOMET_GP, ctx)

    assert len(flags) == 1
    assert flags[0].is_hard_block is True
    assert flags[0].details["component"] == "Glimepiride"


async def test_an_unrelated_allergy_does_not_touch_a_combination() -> None:
    ctx = SafetyContext(
        allergies=[PatientAllergy(allergen_name="Iodine", drug_class="Radiocontrast agent")]
    )

    assert check_allergies(_AUGMENTIN, ctx) == []


# --- interactions reached through an ingredient ------------------------------------------------


async def test_a_curated_pair_fires_through_a_combination_ingredient() -> None:
    """Glycomet GP before a contrast study is the metformin/contrast lactic-acidosis pair."""
    ctx = SafetyContext(current_meds=[_CONTRAST], interaction_rules=[_CONTRAST_METFORMIN])

    flags = check_interactions(_GLYCOMET_GP, ctx)

    assert len(flags) == 1
    assert flags[0].severity == "critical"
    assert flags[0].details["component"] == "Metformin"
    assert flags[0].details["severity"] == "major"


async def test_a_pair_fires_when_the_ingredient_is_on_the_current_medication_side() -> None:
    ctx = SafetyContext(current_meds=[_GLYCOMET_GP], interaction_rules=[_CONTRAST_METFORMIN])

    flags = check_interactions(_CONTRAST, ctx)

    assert len(flags) == 1
    assert flags[0].details["interacting_drug"] == "Metformin + Glimepiride"
    assert flags[0].details["interacting_component"] == "Metformin"


async def test_a_pair_fires_between_two_ingredients_of_two_different_products() -> None:
    """Telma H beside enalapril is dual RAAS blockade, reachable only through telmisartan."""
    ctx = SafetyContext(current_meds=[_ENALAPRIL], interaction_rules=[_ACEI_ARB])

    flags = check_interactions(_TELMA_H, ctx)

    assert len(flags) == 1
    assert "Telmisartan + HCTZ" in flags[0].summary
    assert flags[0].details["component"] == "Telmisartan"


async def test_ingredients_inside_one_product_are_not_flagged_against_each_other() -> None:
    """A licensed fixed-dose combination is a formulation decision already made. Flagging it
    would put an alert on the card that the prescriber cannot act on."""
    intra = InteractionRule(
        drug_a_reference_id="GLM-1",
        drug_b_reference_id="MET-500",
        severity="moderate",
        description="Additive hypoglycaemia.",
    )
    ctx = SafetyContext(current_meds=[_AMLODIPINE], interaction_rules=[intra])

    assert check_interactions(_GLYCOMET_GP, ctx) == []


async def test_the_same_rule_is_reported_once_per_medication_pair() -> None:
    """Two products can meet in one rule by more than one route; one rule is one clinical fact."""
    ctx = SafetyContext(current_meds=[_CONTRAST], interaction_rules=[_CONTRAST_METFORMIN])

    flags = check_interactions(_GLYCOMET_GP, ctx)

    assert len({f.summary for f in flags}) == len(flags) == 1


async def test_a_component_repeating_the_products_own_id_does_not_double_flag() -> None:
    """Curated data can be wrong. A component list that repeats the product's own reference id —
    or the same molecule twice — would otherwise look up one rule several times and put the same
    interaction on the card two or three over, which is how a real finding gets scrolled past."""
    malformed = DrugRef(
        "MET-500",
        "Metformin (mis-seeded)",
        "Biguanide",
        components=(
            DrugRef("MET-500", "Metformin", "Biguanide"),
            DrugRef("MET-500", "Metformin", "Biguanide"),
        ),
    )
    ctx = SafetyContext(current_meds=[_CONTRAST], interaction_rules=[_CONTRAST_METFORMIN])

    assert len(check_interactions(malformed, ctx)) == 1


async def test_reordering_the_same_combination_is_not_an_interaction_with_itself() -> None:
    ctx = SafetyContext(current_meds=[_GLYCOMET_GP], interaction_rules=[_CONTRAST_METFORMIN])

    assert check_interactions(_GLYCOMET_GP, ctx) == []


# --- duplicate therapy across a combination ----------------------------------------------------


async def test_a_single_drug_and_a_combination_containing_it_is_a_duplicate() -> None:
    """Metformin beside Glycomet GP is a doubled metformin dose."""
    flags = check_duplicate_therapy(_METFORMIN, SafetyContext(current_meds=[_GLYCOMET_GP]))

    assert [f.details["match_type"] for f in flags] == ["same_ingredient"]
    assert flags[0].severity == "critical"
    assert flags[0].details["shared_ingredient"] == "Metformin"


async def test_the_duplicate_is_found_from_the_combination_side_too() -> None:
    flags = check_duplicate_therapy(_TELMA_H, SafetyContext(current_meds=[_TELMISARTAN]))

    assert [f.details["match_type"] for f in flags] == ["same_ingredient"]
    assert flags[0].details["shared_ingredient"] == "Telmisartan"


async def test_two_combinations_sharing_a_molecule_with_no_reference_id_still_duplicate() -> None:
    """Matched by name, because hydrochlorothiazide has no standalone row in this vocabulary."""
    other_fdc = DrugRef(
        "AML-HCTZ",
        "Amlodipine + HCTZ",
        "CCB + Thiazide",
        components=(
            DrugRef("AML-5", "Amlodipine", "CCB"),
            DrugRef("", "Hydrochlorothiazide", "Thiazide diuretic"),
        ),
    )

    flags = check_duplicate_therapy(other_fdc, SafetyContext(current_meds=[_TELMA_H]))

    assert flags[0].details["shared_ingredient"] == "Hydrochlorothiazide"


async def test_two_different_unidentified_molecules_are_not_the_same_ingredient() -> None:
    """The trap this fix opens: an ingredient with no standalone vocabulary row carries an empty
    reference id, and comparing empty ids would make clavulanic acid and trimethoprim the same
    molecule — a critical double-dosing flag on two unrelated antibiotics."""
    flags = check_duplicate_therapy(_AUGMENTIN, SafetyContext(current_meds=[_SEPTRAN]))

    assert [f.details["match_type"] for f in flags] != ["same_ingredient"]
    assert all(f.details.get("shared_ingredient") is None for f in flags)


async def test_a_shared_ingredient_class_is_a_therapeutic_duplication() -> None:
    flags = check_duplicate_therapy(_TELMISARTAN, SafetyContext(current_meds=[_TELMA_H]))

    # Telmisartan is shared outright, so the stronger statement wins.
    assert [f.details["match_type"] for f in flags] == ["same_ingredient"]

    # But a different ARB against Telma H is a class duplication reached through the component.
    losartan = DrugRef("LOS-50", "Losartan", "ARB")
    class_flags = check_duplicate_therapy(losartan, SafetyContext(current_meds=[_TELMA_H]))
    assert [f.details["match_type"] for f in class_flags] == ["same_class"]
    assert class_flags[0].details["existing_component"] == "Telmisartan"


async def test_unrelated_products_are_still_not_duplicates() -> None:
    assert check_duplicate_therapy(_AMLODIPINE, SafetyContext(current_meds=[_GLYCOMET_GP])) == []


# --- guideline adherence -----------------------------------------------------------------------


async def test_a_combination_carrying_a_first_line_agent_is_not_a_deviation() -> None:
    """Metformin plus glimepiride is the guideline's own step-up from metformin. Calling it a
    deviation because one of its molecules is a sulfonylurea would have the check contradicting
    the guideline it cites."""
    ctx = SafetyContext(conditions=[PatientCondition(condition_name="Type 2 Diabetes Mellitus")])

    assert check_guideline_adherence(_GLYCOMET_GP, ctx) == []


async def test_a_combination_with_no_first_line_agent_is_still_nudged() -> None:
    glimepiride_only = DrugRef("GLM-1", "Glimepiride", "Sulfonylurea")
    ctx = SafetyContext(conditions=[PatientCondition(condition_name="Type 2 Diabetes Mellitus")])

    flags = check_guideline_adherence(glimepiride_only, ctx)

    assert [f.check_type for f in flags] == ["guideline_deviation"]
    assert flags[0].is_hard_block is False


# --- single-ingredient products are unchanged --------------------------------------------------


async def test_a_single_ingredient_drug_carries_no_component_qualifier() -> None:
    """The whole vocabulary bar eight rows goes through the same code path now; none of it may
    start naming itself as a component of itself."""
    flags = check_contraindications(
        _METFORMIN, SafetyContext(egfr=20.0, contraindication_rules=[_METFORMIN_CKD])
    )

    assert flags[0].summary.startswith("Renal alert: patient eGFR 20.0 mL/min is below 30 for ")
    assert "via its" not in flags[0].summary
    assert "component" not in flags[0].details


async def test_the_reference_id_scope_covers_every_molecule() -> None:
    """The rule tables are queried for the drugs in play. A combination whose components were
    left out of that scope loads none of their rules, and the checks then find nothing."""
    assert ingredient_reference_ids(_GLYCOMET_GP) == {"MET-GLM-1-500", "MET-500", "GLM-1"}
    assert ingredient_reference_ids(_METFORMIN) == {"MET-500"}
    # The empty id of an unidentified molecule is not a queryable key.
    assert ingredient_reference_ids(_TELMA_H) == {"TEL-HCTZ-40", "TEL-40"}


# --- the curated data --------------------------------------------------------------------------


def _vocabulary() -> list[dict]:
    return json.loads(_VOCABULARY.read_text())


async def test_every_combination_row_in_the_seed_declares_its_molecules() -> None:
    """The guard against the next combination being added the way these eight were: a row that
    names several molecules and lists none is a product no rule can reach, and it reports itself
    as clean rather than as unevaluated."""
    missing = [
        row["reference_id"]
        for row in _vocabulary()
        if "+" in row["generic_name"] and not row.get("components")
    ]

    assert missing == [], f"combination rows with no component list: {missing}"


async def test_a_declared_component_list_names_more_than_one_molecule() -> None:
    """A "combination" of one is a data error: it would make the product its own component and
    put a "via its X component" qualifier on a flag about a single-ingredient drug."""
    short = [row["reference_id"] for row in _vocabulary() if len(row.get("components") or []) == 1]

    assert short == [], short


async def test_a_combination_named_without_a_plus_sign_is_still_declared() -> None:
    """The "+" heuristic above is a floor, not a definition. Cotrimoxazole is trimethoprim plus
    sulfamethoxazole under a single name, and a name-shaped rule would never have caught it."""
    by_ref = {row["reference_id"]: row for row in _vocabulary()}

    assert [p["generic_name"] for p in by_ref["TMP-SMX-960"]["components"]] == [
        "Trimethoprim",
        "Sulfamethoxazole",
    ]


async def test_a_component_reference_id_names_a_real_vocabulary_row() -> None:
    """A typo'd id is worse than a null one: it silently applies another drug's rules."""
    rows = _vocabulary()
    known = {row["reference_id"] for row in rows}

    for row in rows:
        for part in row.get("components") or []:
            ref = part.get("reference_id")
            assert ref is None or ref in known, f"{row['reference_id']} -> {ref}"


async def test_a_component_class_matches_the_class_its_own_row_uses() -> None:
    """Class-keyed matching (allergy cross-reactivity, duplicate therapy) compares these strings
    directly, so a component spelling its class differently from its own vocabulary row would
    match nothing while looking correct."""
    rows = _vocabulary()
    by_ref = {row["reference_id"]: row for row in rows}

    for row in rows:
        for part in row.get("components") or []:
            ref = part.get("reference_id")
            if ref is None:
                continue
            assert part.get("drug_class") == by_ref[ref].get("drug_class"), ref


async def test_the_three_motivating_products_carry_the_molecules_they_are_made_of() -> None:
    by_ref = {row["reference_id"]: row for row in _vocabulary()}

    def names(ref: str) -> list[str]:
        return [p["generic_name"] for p in by_ref[ref]["components"]]

    assert names("MET-GLM-1-500") == ["Metformin", "Glimepiride"]
    assert names("TEL-HCTZ-40") == ["Telmisartan", "Hydrochlorothiazide"]
    assert names("AMX-CLV-625") == ["Amoxicillin", "Clavulanic acid"]


# --- the payload parser ------------------------------------------------------------------------


async def test_a_missing_or_malformed_component_payload_costs_only_that_row() -> None:
    """This is on the deterministic safety path: a bad payload must not take the check down."""
    assert _components(None) == ()
    assert _components("not a list") == ()
    assert _components([]) == ()
    assert _components(["a string", 3, None]) == ()
    assert _components([{"reference_id": "X"}]) == ()  # no generic_name
    assert _components([{"generic_name": "  "}]) == ()


async def test_a_component_with_no_reference_id_is_kept_not_dropped() -> None:
    parsed = _components(
        [{"reference_id": None, "generic_name": " Amoxicillin ", "drug_class": "Penicillin"}]
    )

    assert parsed == (DrugRef("", "Amoxicillin", "Penicillin"),)


async def test_a_non_string_field_degrades_rather_than_raising() -> None:
    parsed = _components([{"reference_id": 7, "generic_name": "Amoxicillin", "drug_class": []}])

    assert parsed == (DrugRef("", "Amoxicillin", None),)


# --- through the service -----------------------------------------------------------------------


async def _patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"fdc-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Combination Patient",
        sex="male",
        date_of_birth=date(1968, 4, 11),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _egfr(db, patient: Patient, value: float) -> None:
    lab = LabResult(
        patient_id=patient.id,
        marker_name="Creatinine",
        value_numeric=Decimal("3.4"),
        unit="mg/dL",
        sample_date=date(2026, 3, 1),
    )
    db.add(lab)
    await db.flush()
    db.add(
        DerivedMarker(
            patient_id=patient.id,
            source_lab_result_id=lab.id,
            marker_name="eGFR",
            value_numeric=Decimal(str(value)),
            unit="mL/min/1.73m2",
            formula_name="CKD-EPI 2021",
            formula_version="2021",
            input_values={},
            computed_at=datetime.now(UTC),
        )
    )
    await db.flush()


async def _medication(db, patient: Patient, name: str) -> None:
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


async def test_end_to_end_glycomet_gp_at_egfr_20_is_blocked(db) -> None:
    """The whole point, through the real seed data and the real service. This returned
    ``is_blocked: false`` with an empty flag list."""
    account, patient = await _patient(db)
    await _egfr(db, patient, 20.0)

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="MET-GLM-1-500",
        drug_name=None,
    )

    renal = [f for f in flags if f.check_type == "renal_dose"]
    assert has_hard_block(flags) is True
    assert [f.is_hard_block for f in renal] == [True]
    assert renal[0].details["component"] == "Metformin"


async def test_end_to_end_the_same_patient_on_plain_metformin_is_blocked_identically(db) -> None:
    """The combination and its single-ingredient form must not disagree about the same eGFR."""
    account, patient = await _patient(db)
    await _egfr(db, patient, 20.0)
    service = SafetyService(db)

    _v1, _c1, combo, _i1 = await service.check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="MET-GLM-1-500",
        drug_name=None,
    )
    _v2, _c2, plain, _i2 = await service.check_medication(
        account_id=account.id, patient_id=patient.id, drug_reference_id="MET-500", drug_name=None
    )

    assert has_hard_block(combo) == has_hard_block(plain) is True


async def _allergy(db, patient: Patient, allergen: str) -> None:
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name=allergen,
            allergen_type="drug",
            status="active",
        )
    )
    await db.flush()


async def test_end_to_end_a_sulfonylurea_allergy_blocks_glycomet_gp(db) -> None:
    """Glimepiride resolves exactly to GLM-1. Against MET-GLM-1-500 the reference ids differ,
    "Metformin + Glimepiride" is not "Glimepiride", and "Biguanide + Sulfonylurea" is not
    "Sulfonylurea" — so all three of the pre-ingredient comparisons missed and a patient with a
    documented glimepiride allergy was shown a clean check for a glimepiride product."""
    account, patient = await _patient(db)
    await _allergy(db, patient, "Glimepiride")

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="MET-GLM-1-500",
        drug_name=None,
    )

    allergy_flags = [f for f in flags if f.check_type == "allergy_conflict"]
    assert [f.is_hard_block for f in allergy_flags] == [True]
    assert allergy_flags[0].details["component"] == "Glimepiride"


async def test_end_to_end_a_cephalosporin_allergy_cross_reacts_with_augmentin(db) -> None:
    """The only penicillin in this vocabulary is a combination, and its product-level class is
    "Penicillin + BLI" — a member of no cross-reactivity family. So the penicillin/cephalosporin
    cross-reactivity, the one this curated map exists for, could not fire at all."""
    account, patient = await _patient(db)
    await _allergy(db, patient, "Ceftriaxone")

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="AMX-CLV-625",
        drug_name=None,
    )

    cross = [f for f in flags if f.details.get("match_type") == "cross_reactivity"]
    assert len(cross) == 1
    assert cross[0].details["component"] == "Amoxicillin"
    assert cross[0].is_hard_block is False


async def test_end_to_end_an_allergy_to_the_combination_itself_still_blocks(db) -> None:
    """ "Amoxicillin" resolves to the combination row, so this one blocked before the ingredient
    pass too. It must keep blocking, and it must not sprout a spurious component qualifier."""
    account, patient = await _patient(db)
    await _allergy(db, patient, "Amoxicillin")

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="AMX-CLV-625",
        drug_name=None,
    )

    allergy_flags = [f for f in flags if f.check_type == "allergy_conflict"]
    assert [f.is_hard_block for f in allergy_flags] == [True]
    assert "component" not in allergy_flags[0].details


async def test_end_to_end_telma_40_beside_telma_h_is_a_doubled_dose(db) -> None:
    account, patient = await _patient(db)
    await _medication(db, patient, "Telmisartan")

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="TEL-HCTZ-40",
        drug_name=None,
    )

    duplicates = [f for f in flags if f.check_type == "duplicate_therapy"]
    assert [f.details["match_type"] for f in duplicates] == ["same_ingredient"]
    assert duplicates[0].details["shared_ingredient"] == "Telmisartan"


async def test_end_to_end_the_ingredient_rules_are_actually_loaded_into_the_context(db) -> None:
    """The scope guard. If ``_build_context`` queried only the product's own reference id, the
    metformin rules would never reach the engine and every check above would pass vacuously."""
    account, patient = await _patient(db)
    await _egfr(db, patient, 20.0)

    _vocab, ctx, _flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="MET-GLM-1-500",
        drug_name=None,
    )

    assert "MET-500" in {r.drug_reference_id for r in ctx.contraindication_rules}


async def test_end_to_end_components_survive_a_medication_linked_only_by_name(db) -> None:
    """A row imported before the vocabulary knew the brand resolves by name. Which rules fire
    must not depend on how the row happened to be linked."""
    account, patient = await _patient(db)
    await _medication(db, patient, "Glycomet GP")  # brand name, no drug_vocabulary_id

    ctx = await SafetyService(db)._build_context(patient.id)

    combo = next(m for m in ctx.current_meds if m.reference_id == "MET-GLM-1-500")
    assert [c.generic_name for c in combo.components] == ["Metformin", "Glimepiride"]


async def test_end_to_end_a_current_combination_interacts_with_a_proposal(db) -> None:
    """Glycomet GP on the chart, iodinated contrast proposed: the seeded major interaction is
    keyed on MET-500 and was unreachable from either side."""
    account, patient = await _patient(db)
    await _medication(db, patient, "Glycomet GP")

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id="CONTRAST-IODINE",
        drug_name=None,
    )

    interactions = [f for f in flags if f.check_type == "drug_interaction"]
    assert [f.details["severity"] for f in interactions] == ["major"]
    assert interactions[0].details["interacting_component"] == "Metformin"


async def test_end_to_end_a_single_ingredient_drug_is_unaffected(db) -> None:
    """Forty-two of the fifty seeded rows carry no components at all."""
    account, patient = await _patient(db)
    await _egfr(db, patient, 90.0)

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id, patient_id=patient.id, drug_reference_id="AML-5", drug_name=None
    )

    assert has_hard_block(flags) is False
    assert all("component" not in f.details for f in flags)


async def test_the_vocabulary_row_reaches_the_engine_with_its_components(db) -> None:
    """``_drug_ref`` is the single seam; a hand-built ``DrugRef`` anywhere silently un-does all
    of the above."""
    resolved = await SafetyService(db).resolver.resolve_reference_ids({"MET-GLM-1-500"})

    ref = _drug_ref(resolved["MET-GLM-1-500"])

    assert ingredient_reference_ids(ref) == {"MET-GLM-1-500", "MET-500", "GLM-1"}


async def test_the_whole_engine_blocks_a_combination_it_used_to_pass() -> None:
    """Through ``evaluate_drug_safety``, not one check in isolation — a check nothing calls is
    the failure this file's neighbours were written for."""
    ctx = SafetyContext(
        egfr=20.0,
        current_meds=[_CONTRAST],
        contraindication_rules=[_METFORMIN_CKD],
        interaction_rules=[_CONTRAST_METFORMIN],
    )

    flags = evaluate_drug_safety(_GLYCOMET_GP, ctx)

    assert has_hard_block(flags) is True
    assert {"renal_dose", "drug_interaction"} <= {f.check_type for f in flags}


async def test_no_flag_about_a_combination_uses_imperative_clinical_language() -> None:
    """CLAUDE.md safety rule #4, checked on the generated text rather than on the template."""
    ctx = SafetyContext(
        egfr=20.0,
        current_meds=[_CONTRAST],
        allergies=[PatientAllergy(allergen_name="Glimepiride", allergy_id="a1")],
        contraindication_rules=[_METFORMIN_CKD],
        interaction_rules=[_CONTRAST_METFORMIN],
    )

    for flag in evaluate_drug_safety(_GLYCOMET_GP, ctx):
        for banned in ("Give ", "Administer ", "The patient has ", "Diagnose with "):
            assert banned not in flag.summary
