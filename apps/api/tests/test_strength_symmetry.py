"""Which strength of a molecule was prescribed must not decide what the safety engine says.

Every curated rule in this engine is keyed on a *reference id*, and a reference id is a molecule
at a strength: Ecosprin 75 and Ecosprin 150 are two vocabulary rows for aspirin, Crocin 500 and
Crocin 650 two rows for paracetamol. Six of the fifty seeded products are one of a pair like
that, and they share a brand name, so which row a prescription resolves to is decided by row
order in the table rather than by anything on the page.

That made curation drift a clinical failure with no symptom. The interaction table had exactly
that drift — warfarin curated against ASP-75 and not ASP-150 — and it is now pinned by
``test_interaction_severity``. This file pins the same invariant across the tables that were left
relying on the curator's memory, where the stakes are higher:

  * **contraindications**, which produce *hard blocks* (Critical Safety Rule #3). A rule curated
    on one strength and not the other is an absolute contraindication that a clinician is stopped
    by or waved through depending on which tablet size the chart happened to name.
  * **the per-row clinical attributes of the vocabulary itself** — ``drug_class`` (allergy
    cross-reactivity and duplicate-therapy both key on it), ``hepatotoxicity`` (the burden count),
    ``components`` (every rule a combination reaches through its molecules), ``is_active``.

And then the property those two are only a proxy for, asserted end to end through
``evaluate_drug_safety`` against the real seeded reference data: two strengths of one molecule,
one identical patient, the same flags. That is the assertion that survives someone adding a
seventh per-row field, because it does not enumerate the fields.

The tables are symmetric today. The point is that "some strengths of this molecule are covered"
is invisible from the screen, so it has to be visible from CI.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest

from app.core.safety import (
    ContraindicationRule,
    DrugRef,
    HepaticPanel,
    InteractionRule,
    PatientAllergy,
    PatientCondition,
    SafetyContext,
    evaluate_drug_safety,
)

_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "drugs"


def _load(name: str) -> list[dict]:
    return json.loads((_DATA_DIR / name).read_text())


VOCABULARY = _load("drug_vocabulary.json")
INTERACTIONS = _load("interactions.json")
CONTRAINDICATIONS = _load("contraindications.json")

_BY_REFERENCE_ID = {row["reference_id"]: row for row in VOCABULARY}

# The per-row fields a safety check reads. Deliberately not "every key in the row": strength and
# form are what *distinguish* the two rows, and dosage text is presentation.
_CLINICAL_ATTRIBUTES = ("drug_class", "hepatotoxicity", "components", "is_active", "atc_code")


def _multi_strength() -> dict[str, list[str]]:
    """generic name -> its reference ids, for molecules seeded at more than one strength."""
    by_generic: dict[str, list[str]] = collections.defaultdict(list)
    for row in VOCABULARY:
        by_generic[row["generic_name"]].append(row["reference_id"])
    return {g: sorted(refs) for g, refs in by_generic.items() if len(refs) > 1}


MULTI_STRENGTH = _multi_strength()


def test_the_corpus_still_has_molecules_seeded_at_more_than_one_strength():
    """Every assertion below is vacuous if this stops being true, and it would stop being true
    silently — the tests would all pass on an empty parametrize."""
    assert len(MULTI_STRENGTH) >= 5, MULTI_STRENGTH


# --- The curated tables --------------------------------------------------------------------------


def _contraindications_by_reference_id() -> dict[str, set[tuple]]:
    """reference id -> the comparable content of each rule curated against it.

    Compared as a set of tuples rather than by identity, because two rules for the same molecule
    at two strengths are different rows saying the same clinical thing. Thresholds are folded to
    their JSON so a nested dict compares by value.
    """
    by_id: dict[str, set[tuple]] = collections.defaultdict(set)
    for rule in CONTRAINDICATIONS:
        by_id[rule["drug_reference_id"]].add(
            (
                rule["condition_name"].strip().lower(),
                rule["severity"],
                bool(rule["is_absolute"]),
                json.dumps(rule.get("renal_threshold"), sort_keys=True),
                json.dumps(rule.get("hepatic_threshold"), sort_keys=True),
                (rule.get("icd10_code") or "").strip().lower(),
            )
        )
    return by_id


def test_every_strength_of_a_molecule_carries_the_same_contraindications():
    """A contraindication is a fact about a molecule, and it is the one that hard-blocks.

    Curating "metformin is contraindicated below eGFR 30" against one strength and not the other
    means the block a clinician cannot proceed past is present or absent depending on which
    tablet size the prescription named — and the absent case reports ``is_blocked: false``, which
    reads exactly like a chart that was checked and cleared.

    Asserted per molecule so the failure message names the drug a curator has to go and look at.
    """
    curated = _contraindications_by_reference_id()
    mismatched = {
        generic: {ref: sorted(curated.get(ref, set())) for ref in refs}
        for generic, refs in MULTI_STRENGTH.items()
        if len({frozenset(curated.get(ref, set())) for ref in refs}) > 1
    }
    assert mismatched == {}


@pytest.mark.parametrize("attribute", _CLINICAL_ATTRIBUTES)
def test_every_strength_of_a_molecule_carries_the_same_clinical_attributes(attribute):
    """The rules are not the only thing keyed per row.

    ``drug_class`` decides allergy cross-reactivity and same-class duplicate therapy;
    ``hepatotoxicity`` decides whether a drug counts toward the liver-injury burden;
    ``components`` decides every rule a combination product reaches through its molecules. Each
    is stored on the vocabulary row, so each can drift between two strengths of one molecule the
    same way the interaction table did, with the same symptom of none at all.
    """
    mismatched = {
        generic: {ref: _BY_REFERENCE_ID[ref].get(attribute) for ref in refs}
        for generic, refs in MULTI_STRENGTH.items()
        if len({json.dumps(_BY_REFERENCE_ID[ref].get(attribute), sort_keys=True) for ref in refs})
        > 1
    }
    assert mismatched == {}


def test_every_contraindication_names_a_real_vocabulary_row():
    """A rule keyed on a reference id that no product has can never fire, and nothing else says
    so — it is a curated safety rule that is inert on every chart."""
    unknown = {
        rule["drug_reference_id"]
        for rule in CONTRAINDICATIONS
        if rule["drug_reference_id"] not in _BY_REFERENCE_ID
    }
    assert unknown == set()


def test_an_absolute_contraindication_is_curated_as_one():
    """``is_absolute`` and ``severity`` are two fields saying the same thing, and the engine hard-
    blocks off the first. A row where they disagree is a rule whose rendered weight is not the
    weight the curator wrote down."""
    disagreeing = [
        (rule["drug_reference_id"], rule["condition_name"], rule["severity"], rule["is_absolute"])
        for rule in CONTRAINDICATIONS
        if bool(rule["is_absolute"]) != (rule["severity"] == "absolute")
    ]
    assert disagreeing == []


# --- The property the two above are a proxy for --------------------------------------------------


def _drug_ref(reference_id: str) -> DrugRef:
    """A DrugRef built from a real seeded row, components and all."""
    row = _BY_REFERENCE_ID[reference_id]
    return DrugRef(
        reference_id=row["reference_id"],
        generic_name=row["generic_name"],
        drug_class=row.get("drug_class"),
        hepatotoxicity=row.get("hepatotoxicity"),
        components=tuple(
            DrugRef(
                reference_id=c.get("reference_id") or "",
                generic_name=c["generic_name"],
                drug_class=c.get("drug_class"),
                hepatotoxicity=c.get("hepatotoxicity"),
            )
            for c in (row.get("components") or [])
        ),
    )


def _all_interaction_rules() -> list[InteractionRule]:
    return [
        InteractionRule(
            drug_a_reference_id=r["drug_a_reference_id"],
            drug_b_reference_id=r["drug_b_reference_id"],
            severity=r["severity"],
            description=r["description"],
            management=r.get("management"),
        )
        for r in INTERACTIONS
    ]


def _all_contraindication_rules() -> list[ContraindicationRule]:
    return [
        ContraindicationRule(
            drug_reference_id=r["drug_reference_id"],
            condition_name=r["condition_name"],
            severity=r["severity"],
            description=r["description"],
            is_absolute=bool(r["is_absolute"]),
            renal_threshold=r.get("renal_threshold"),
            hepatic_threshold=r.get("hepatic_threshold"),
            icd10_code=r.get("icd10_code"),
        )
        for r in CONTRAINDICATIONS
    ]


def _stressed_context() -> SafetyContext:
    """One patient built to make as many checks as possible fire at once.

    The whole seeded corpus is on board, the conditions cover every key the contraindication
    table curates, renal and hepatic function are impaired enough to reach the threshold rules,
    and there are documented allergies in two widely shared classes. The point is not that this
    patient is realistic — it is that a difference between two strengths anywhere in the engine
    has somewhere to show up, and a molecule with no curated rule of its own still reaches the
    duplicate-therapy and allergy paths rather than agreeing vacuously at zero flags.

    Every product, not just the interaction partners: a combination is where a multi-strength
    molecule is reached through a component rather than through its own row (Glycomet GP carries
    glimepiride as GLM-1, so proposing GLM-2 has to find it by generic name), and that path is
    exactly the one that would drift silently.
    """
    return SafetyContext(
        current_meds=[_drug_ref(row["reference_id"]) for row in VOCABULARY],
        allergies=[
            PatientAllergy(allergen_name="Penicillin", drug_class="Penicillin"),
            PatientAllergy(allergen_name="Aspirin", drug_class="Antiplatelet"),
        ],
        conditions=[
            PatientCondition(condition_name=rule["condition_name"]) for rule in CONTRAINDICATIONS
        ],
        egfr=22.0,
        hepatic=HepaticPanel(bilirubin_mg_dl=3.4, alt_u_l=180.0, albumin_g_dl=2.6, inr=2.1),
        interaction_rules=_all_interaction_rules(),
        contraindication_rules=_all_contraindication_rules(),
    )


def _comparable(flags) -> list[tuple]:
    """A flag reduced to what it means, with the drug's own name factored out.

    The summary text names the product, and two strengths of one molecule share a generic name,
    so the strings coincide — but comparing them would still make this test about wording. What
    is asserted instead is the clinical content: which check fired, how severely, whether it
    stops the prescription, and which curated rule it came from.
    """
    return sorted(
        (
            flag.check_type,
            flag.severity,
            flag.is_hard_block,
            str(flag.details.get("component") or ""),
            str(flag.details.get("interacting_drug") or ""),
            str(flag.details.get("condition") or flag.details.get("allergen") or ""),
        )
        for flag in flags
    )


@pytest.mark.parametrize(
    "generic,reference_ids",
    sorted(MULTI_STRENGTH.items()),
    ids=sorted(MULTI_STRENGTH),
)
def test_both_strengths_of_a_molecule_produce_the_same_safety_flags(generic, reference_ids):
    """The end-to-end form, and the one that does not have to enumerate the fields.

    Same patient, same reference data, the two strengths proposed in turn. Anything that differs
    between the rows — a rule curated on one, a class typo, a hepatotoxicity tier set on one and
    not the other, a component list on one — comes out here as a flag present in one run and
    absent in the other, which is precisely the shape of the failure: not a weaker warning, a
    missing one.
    """
    ctx = _stressed_context()
    verdicts = {
        ref: _comparable(evaluate_drug_safety(_drug_ref(ref), ctx)) for ref in reference_ids
    }

    first = verdicts[reference_ids[0]]
    for ref in reference_ids[1:]:
        assert verdicts[ref] == first, f"{generic}: {reference_ids[0]} and {ref} disagree"


@pytest.mark.parametrize(
    "generic,reference_ids",
    sorted(MULTI_STRENGTH.items()),
    ids=sorted(MULTI_STRENGTH),
)
def test_the_stressed_patient_actually_makes_the_checks_fire(generic, reference_ids):
    """Guards the test above from passing because nothing happened.

    Two strengths agree trivially when both produce no flags at all, and that is not a
    hypothetical: with only the interaction partners on board, three of the six molecules here
    have no curated rule of their own and returned an empty verdict, so their symmetry was being
    asserted about nothing. Parametrised per molecule so a future corpus addition that nothing
    can reach names itself.
    """
    ctx = _stressed_context()
    for ref in reference_ids:
        assert evaluate_drug_safety(_drug_ref(ref), ctx), f"{generic} ({ref}) reached no check"


def test_the_stressed_patient_reaches_the_hard_block_path():
    """The check whose absence matters most, exercised through the real seeded rules rather than
    through a fixture written to make it fire."""
    ctx = _stressed_context()
    metformin = evaluate_drug_safety(_drug_ref("MET-500"), ctx)
    assert any(f.is_hard_block for f in metformin)
    assert "contraindication" in {f.check_type for f in metformin}


def test_a_combination_reaches_a_multi_strength_molecule_through_its_component():
    """The path the fixture was widened to cover.

    Glycomet GP carries glimepiride as GLM-1. Proposing the *other* strength has to find that
    duplication by generic name, because the reference ids differ — and a chart on which one
    strength is flagged and the other is not is the failure this whole file is about.
    """
    ctx = SafetyContext(current_meds=[_drug_ref("MET-GLM-1-500")])
    for ref in ("GLM-1", "GLM-2"):
        flags = evaluate_drug_safety(_drug_ref(ref), ctx)
        shared = [f for f in flags if f.details.get("match_type") == "same_ingredient"]
        assert shared, ref
        assert shared[0].details["shared_ingredient"] == "Glimepiride"
