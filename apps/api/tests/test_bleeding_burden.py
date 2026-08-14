"""Bleeding risk is a property of the medication *set*, and the pairwise table cannot say so.

``check_interactions`` grades one curated pair at a time. That is the right shape for "this
antibiotic raises this patient's INR" and the wrong shape for the commonest dangerous
polypharmacy in ambulatory practice: a chart carrying several drugs that each independently
impair haemostasis.

Take warfarin, aspirin, clopidogrel and diclofenac on one chart — not an exotic combination, it
is what accumulates on an elderly cardiac patient who also has arthritis. That is six pairs. The
curated table carries three of them (aspirin+warfarin, diclofenac+warfarin, aspirin+diclofenac),
each graded "major". So the screen shows three findings, each carrying exactly the weight a
single warfarin+aspirin chart would show, and nothing anywhere states the actual clinical
picture: four agents, three mechanisms, a bleeding risk greater than any pair in the set.

Two of the missing pairs were missing by oversight and are now curated — clopidogrel+warfarin had
no rule at all, and ibuprofen was missing the aspirin rule its fellow NSAID diclofenac carried.
But the third is missing *on purpose*: dual antiplatelet therapy is deliberately prescribed after
a stent, and curating aspirin+clopidogrel as an interaction would fire an amber card on correct
treatment. It is the third and fourth agent that changes the picture, and a table of pairs has no
way to express "this pair is fine, and these four together are not".

Which is the argument ``check_duplicate_therapy`` and ``check_hepatotoxic_burden`` already made
on their own axes, and is why this check exists rather than more rows in the table.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest

from app.core.safety import (
    DrugRef,
    InteractionRule,
    SafetyContext,
    check_bleeding_burden,
    check_interactions,
    evaluate_drug_safety,
)

_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "drugs"
VOCABULARY = json.loads((_DATA_DIR / "drug_vocabulary.json").read_text())
INTERACTIONS = json.loads((_DATA_DIR / "interactions.json").read_text())
_BY_REFERENCE_ID = {row["reference_id"]: row for row in VOCABULARY}


def _drug(reference_id: str) -> DrugRef:
    row = _BY_REFERENCE_ID[reference_id]
    return DrugRef(
        reference_id=row["reference_id"],
        generic_name=row["generic_name"],
        drug_class=row.get("drug_class"),
        hepatotoxicity=row.get("hepatotoxicity"),
    )


def _ctx(*current: str) -> SafetyContext:
    return SafetyContext(current_meds=[_drug(ref) for ref in current])


def _flag(proposed: str, *current: str):
    flags = check_bleeding_burden(_drug(proposed), _ctx(*current))
    return flags[0] if flags else None


# --- When it fires, and when it stays quiet -------------------------------------------------------


def test_one_bleeding_drug_on_an_otherwise_clean_chart_is_not_a_finding():
    """Aspirin alone is the single most prescribed drug in this vocabulary's market. A cumulative
    check that fires on a set of one would be an amber card on every cardiac chart."""
    assert _flag("ASP-75", "MET-500", "TEL-40") is None


def test_a_drug_that_does_not_bleed_is_not_flagged_by_a_chart_full_of_drugs_that_do():
    """The proposed drug has to be part of the set. Levothyroxine on a triple-therapy chart is
    not a bleeding decision, and flagging it would put the finding on the wrong card."""
    assert _flag("LT4-50", "WARF-5", "ASP-75", "CLO-75") is None


def test_a_drug_whose_class_is_not_curated_is_never_counted_as_safe():
    """Silence about metformin means the question was not asked, not that it was answered no —
    the same contract ``hepatotoxicity is None`` carries. So a chart of uncurated drugs beside
    one anticoagulant produces nothing rather than "warfarin is the only bleeding risk here"."""
    assert _flag("WARF-5", "MET-500", "LT4-50", "PAN-40") is None


def test_re_ordering_the_same_drug_does_not_manufacture_a_second_agent():
    """Aspirin proposed for a patient already on aspirin is a duplicate — which
    ``check_duplicate_therapy`` reports — and must not read here as two antiplatelets."""
    assert _flag("ASP-75", "ASP-75") is None


def test_two_potentiators_alone_are_not_a_bleeding_set():
    """A corticosteroid does not impair haemostasis; it makes another agent's GI bleed worse.
    A chart carrying only potentiators has nothing for them to potentiate."""
    assert _flag("PRD-10", "PRD-10") is None


def test_a_corticosteroid_on_top_of_an_nsaid_is_a_finding():
    """The other half of the same rule. Steroid plus NSAID is one of the best-documented
    upper-GI-bleed combinations there is, and no curated pair covers it."""
    flag = _flag("PRD-10", "DIC-50")
    assert flag is not None
    assert flag.severity == "warning"
    assert "Diclofenac" in flag.details["concurrent_bleeding_drugs"]


# --- The grading ----------------------------------------------------------------------------------


def test_two_antiplatelets_are_a_warning_not_a_critical():
    """Dual antiplatelet therapy after a stent is correct treatment. It is worth naming — it is
    still two agents — but grading it the same as triple therapy would train the clinician to
    dismiss the card that matters."""
    flag = _flag("ASP-75", "CLO-75")
    assert flag is not None
    assert flag.severity == "warning"
    assert flag.is_hard_block is False


def test_an_anticoagulant_plus_any_second_agent_is_critical():
    """The combination that fills medical wards. Two agents, but one of them stops the
    coagulation cascade, so it does not grade with aspirin-plus-clopidogrel."""
    for partner in ("ASP-75", "CLO-75", "DIC-50", "IBU-400"):
        flag = _flag("WARF-5", partner)
        assert flag is not None, partner
        assert flag.severity == "critical", partner
        assert flag.details["includes_anticoagulant"] is True


def test_three_antiplatelet_class_agents_are_critical_without_any_anticoagulant():
    """Count alone is enough. Aspirin, clopidogrel and an NSAID is triple platelet inhibition,
    and no pair in it carries an anticoagulant for the rule above to catch."""
    flag = _flag("ASP-75", "CLO-75", "IBU-400")
    assert flag is not None
    assert flag.severity == "critical"
    assert flag.details["includes_anticoagulant"] is False
    assert flag.details["haemostatic_agent_count"] == 3


def test_a_potentiator_does_not_by_itself_escalate_the_grade():
    """Prednisolone alongside two antiplatelets makes three drugs and two haemostatic agents.
    Grading on the drug count would call that triple therapy; it is not."""
    flag = _flag("ASP-75", "CLO-75", "PRD-10")
    assert flag is not None
    assert flag.details["haemostatic_agent_count"] == 2
    assert flag.severity == "warning"


def test_the_finding_is_never_a_hard_block():
    """Triple therapy after a stent in a patient with atrial fibrillation is a real,
    guideline-supported prescription. Refusing it would be wrong."""
    flag = _flag("WARF-5", "ASP-75", "CLO-75", "DIC-50")
    assert flag is not None
    assert flag.severity == "critical"
    assert flag.is_hard_block is False


# --- What it says ---------------------------------------------------------------------------------


def test_the_summary_names_every_other_agent_and_counts_the_mechanisms():
    flag = _flag("WARF-5", "ASP-75", "CLO-75", "DIC-50")
    assert flag is not None
    for name in ("Aspirin", "Clopidogrel", "Diclofenac"):
        assert name in flag.summary, flag.summary
    assert flag.details["haemostatic_agent_count"] == 4
    assert set(flag.details["concurrent_bleeding_drugs"]) == {
        "Aspirin",
        "Clopidogrel",
        "Diclofenac",
    }
    assert len(flag.details["bleeding_mechanisms"]) == 3


def test_the_summary_says_why_it_is_not_just_the_pairs_again():
    """The whole reason the check exists. Without this sentence the card reads as a fourth
    interaction warning rather than as a statement about the set."""
    flag = _flag("WARF-5", "ASP-75", "CLO-75")
    assert flag is not None
    assert "one pair at a time" in flag.summary
    assert "no curated rule at all" in flag.summary


def test_the_summary_admits_the_bound_of_what_it_scored():
    """Same caveat ``check_hepatotoxic_burden`` carries, for the same reason: fifty seeded drugs
    are not a formulary, and "2 bleeding-risk drugs" must not read as "and no others"."""
    flag = _flag("WARF-5", "ASP-75")
    assert flag is not None
    assert "were not counted either way" in flag.summary


def test_the_wording_is_prescriber_framed():
    """Critical Safety Rule #4 — no imperative clinical language anywhere in the output."""
    flag = _flag("WARF-5", "ASP-75", "CLO-75", "DIC-50")
    assert flag is not None
    assert "Guidelines support" in flag.summary
    for imperative in ("Give ", "Administer ", "Stop the ", "Discontinue ", "The patient has "):
        assert imperative not in flag.summary


# --- Fixed-dose combinations ----------------------------------------------------------------------


def test_a_combination_tablet_counts_the_molecules_it_contains():
    """A single aspirin+clopidogrel tablet is two antiplatelets, and its product class names
    neither. Written against the product alone this reads one drug where the patient takes two —
    the same failure the whole ``_ingredients`` pass exists to prevent."""
    combo = DrugRef(
        reference_id="ASP-CLO-75",
        generic_name="Aspirin + Clopidogrel",
        drug_class="Antiplatelet combination",
        components=(
            DrugRef(reference_id="ASP-75", generic_name="Aspirin", drug_class="Antiplatelet"),
            DrugRef(reference_id="CLO-75", generic_name="Clopidogrel", drug_class="Antiplatelet"),
        ),
    )
    flags = check_bleeding_burden(combo, _ctx("WARF-5"))

    assert len(flags) == 1
    # Three haemostatic agents from two prescriptions: the combination is not one drug here.
    assert flags[0].details["haemostatic_agent_count"] == 3
    assert flags[0].severity == "critical"


# --- The gap it closes, stated against the real seeded table ------------------------------------


def _seeded_pair_severity(a: str, b: str) -> str | None:
    rules = [
        InteractionRule(
            r["drug_a_reference_id"], r["drug_b_reference_id"], r["severity"], r["description"]
        )
        for r in INTERACTIONS
    ]
    flags = check_interactions(
        _drug(a), SafetyContext(current_meds=[_drug(b)], interaction_rules=rules)
    )
    return flags[0].details["severity"] if flags else None


def test_the_four_drug_chart_produces_more_pairs_than_the_table_carries():
    """The measurement behind this file's docstring, taken against the shipped table rather than
    asserted. Six pairs; the table speaks to four of them and is silent on two."""
    chart = ["WARF-5", "ASP-75", "CLO-75", "DIC-50"]
    pairs = [(a, b) for i, a in enumerate(chart) for b in chart[i + 1 :]]
    assert len(pairs) == 6

    covered = {pair for pair in pairs if _seeded_pair_severity(*pair) is not None}
    assert len(covered) == 4
    # Deliberately uncurated: dual antiplatelet therapy, and clopidogrel with an NSAID.
    assert {frozenset(p) for p in pairs} - {frozenset(p) for p in covered} == {
        frozenset({"ASP-75", "CLO-75"}),
        frozenset({"CLO-75", "DIC-50"}),
    }


def test_the_set_level_finding_is_the_only_one_that_scales_with_the_chart():
    """Adding a fourth antithrombotic adds pairwise findings that each still read as one pair.
    The burden flag is the one thing on the screen whose wording changes."""
    three = check_bleeding_burden(_drug("WARF-5"), _ctx("ASP-75", "CLO-75"))
    four = check_bleeding_burden(_drug("WARF-5"), _ctx("ASP-75", "CLO-75", "DIC-50"))

    assert three[0].details["haemostatic_agent_count"] == 3
    assert four[0].details["haemostatic_agent_count"] == 4
    assert three[0].summary != four[0].summary


def test_clopidogrel_and_warfarin_are_now_a_curated_pair_as_well():
    """The rule that was absent entirely. The burden check would have caught the set, but a
    two-drug chart of exactly these two is a real prescription and deserves its own finding."""
    assert _seeded_pair_severity("CLO-75", "WARF-5") == "major"


@pytest.mark.parametrize("aspirin", ["ASP-75", "ASP-150"])
def test_both_nsaids_carry_the_aspirin_rule_their_shared_class_implies(aspirin):
    """Diclofenac had it and ibuprofen did not, at both aspirin strengths.

    Not generalised into a blanket "same class implies the same partners" invariant, because
    that is false where the mechanism is pharmacokinetic rather than class-wide: clopidogrel
    interacts with the PPIs through CYP2C19 and aspirin, its fellow antiplatelet, does not.
    Diclofenac and ibuprofen are interchangeable for every rule curated here, so this pair of
    molecules is pinned by name.
    """
    assert _seeded_pair_severity(aspirin, "IBU-400") == "major"
    assert _seeded_pair_severity(aspirin, "DIC-50") == "major"


def test_the_two_nsaids_carry_exactly_the_same_partners():
    partners: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for rule in INTERACTIONS:
        a, b = rule["drug_a_reference_id"], rule["drug_b_reference_id"]
        partners[a][_BY_REFERENCE_ID[b]["generic_name"]] = rule["severity"]
        partners[b][_BY_REFERENCE_ID[a]["generic_name"]] = rule["severity"]

    assert partners["DIC-50"] == partners["IBU-400"]


# --- Wiring ---------------------------------------------------------------------------------------


def test_the_check_runs_as_part_of_the_standard_evaluation():
    """A check nothing calls is a check that does not exist."""
    flags = evaluate_drug_safety(_drug("WARF-5"), _ctx("ASP-75", "CLO-75"))
    assert any(f.check_type == "bleeding_burden" for f in flags)


def test_the_check_type_is_one_the_audit_table_will_accept():
    """``drug_safety_checks`` carries a CHECK constraint on this column, so a flag type outside
    it is an integrity error at persist time rather than a bad card."""
    from app.models.drug_safety_check import DrugSafetyCheck

    constraint = next(c for c in DrugSafetyCheck.__table_args__ if c.name == "ck_dsc_check_type")
    assert "bleeding_burden" in str(constraint.sqltext)
