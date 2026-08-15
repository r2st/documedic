"""How much clinical weight a curated interaction carries, and that the weight survives.

A drug interaction is not one thing. "Aspirin on top of warfarin" is a bleeding risk a
prescriber has to weigh; "amoxicillin-clavulanate alongside paracetamol" is curated only so the
table can say there is nothing to worry about. Both are rows in the same table, and the engine
grades them apart — contraindicated hard-blocks, major is critical, moderate is a warning, minor
is informational — so a chart shows the first as red and the second as a blue note.

That grading is only as good as two things underneath it, and this file tests both:

  * the mapping from a rule's curated severity to a flag's, which decides how the finding renders
    and where it sorts; and
  * the curated table itself, which decides whether a finding exists at all.

The second is where this went wrong. A rule is keyed on a *reference id*, and a reference id is
strength-specific: Ecosprin 75 and Ecosprin 150 are two rows for one molecule. The warfarin
interaction was curated against ASP-75 only, so a patient on Ecosprin 150 and warfarin — the
exact pair the table's most emphatic entry is about — got no flag at all. Not a weaker flag: no
flag, which renders as a clean check. Atorvastatin and paracetamol had drifted the same way.

So the symmetry is now an invariant with a test on it rather than a habit the curator has to
remember, because "some strengths of this molecule are covered" is invisible from the screen.
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
    check_interactions,
    has_hard_block,
)

_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "drugs"


def _load(name: str) -> list[dict]:
    return json.loads((_DATA_DIR / name).read_text())


VOCABULARY = _load("drug_vocabulary.json")
INTERACTIONS = _load("interactions.json")

# The severities the DB check constraint on drug_interactions admits.
CURATED_SEVERITIES = ("minor", "moderate", "major", "contraindicated")

_BY_REFERENCE_ID = {row["reference_id"]: row for row in VOCABULARY}


def _drug(reference_id: str) -> DrugRef:
    """A DrugRef for a real seeded vocabulary row."""
    row = _BY_REFERENCE_ID[reference_id]
    return DrugRef(
        reference_id=row["reference_id"],
        generic_name=row["generic_name"],
        drug_class=row.get("drug_class"),
    )


def _rules() -> list[InteractionRule]:
    return [
        InteractionRule(
            r["drug_a_reference_id"],
            r["drug_b_reference_id"],
            r["severity"],
            r["description"],
        )
        for r in INTERACTIONS
    ]


def _flag_for(proposed: str, current: str):
    """The single interaction flag between two seeded drugs, evaluated against the real table."""
    ctx = SafetyContext(current_meds=[_drug(current)], interaction_rules=_rules())
    flags = check_interactions(_drug(proposed), ctx)
    return flags[0] if flags else None


# --- The grading itself --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_severity,expected_severity,expected_hard",
    [
        ("contraindicated", "hard_block", True),
        ("major", "critical", False),
        ("moderate", "warning", False),
        ("minor", "info", False),
    ],
)
def test_each_curated_severity_maps_to_its_own_clinical_weight(
    rule_severity, expected_severity, expected_hard
):
    """Four curated tiers, four distinct flags. Collapsing any two would make a bleeding risk
    and a "nothing to see here" note render identically."""
    ctx = SafetyContext(
        current_meds=[_drug("WARF-5")],
        interaction_rules=[InteractionRule("ASP-75", "WARF-5", rule_severity, "x")],
    )
    flags = check_interactions(_drug("ASP-75"), ctx)

    assert len(flags) == 1
    assert flags[0].severity == expected_severity
    assert flags[0].is_hard_block is expected_hard


def test_a_severity_the_engine_does_not_know_is_graded_as_a_warning():
    """Defensive, and deliberately not "info": a tier this build has not been taught is not
    grounds for rendering it as the quietest thing on the screen. The DB check constraint should
    keep such a row out, so this is the behaviour if that constraint is ever relaxed."""
    ctx = SafetyContext(
        current_meds=[_drug("WARF-5")],
        interaction_rules=[InteractionRule("ASP-75", "WARF-5", "catastrophic", "x")],
    )
    flags = check_interactions(_drug("ASP-75"), ctx)

    assert len(flags) == 1
    assert flags[0].severity == "warning"
    assert flags[0].is_hard_block is False


def test_a_major_interaction_outranks_a_minor_one_on_the_same_chart():
    """The property the grading exists for, stated end to end against the seeded table.

    Warfarin with aspirin is a major bleeding risk; amoxicillin-clavulanate with paracetamol is
    curated as minor precisely because there is nothing to act on. If those two produced flags of
    the same weight, the table's tiers would be decoration.
    """
    major = _flag_for("ASP-75", "WARF-5")
    minor = _flag_for("PCM-500", "AMX-CLV-625")

    assert major is not None and minor is not None
    assert major.severity == "critical"
    assert minor.severity == "info"

    order = ["hard_block", "critical", "warning", "info"]
    assert order.index(major.severity) < order.index(minor.severity)


def test_the_only_contraindicated_pair_is_the_one_that_hard_blocks():
    """A hard block cannot be prescribed past without a documented override, so exactly the rules
    curated as contraindicated may produce one."""
    contraindicated = {
        (r["drug_a_reference_id"], r["drug_b_reference_id"])
        for r in INTERACTIONS
        if r["severity"] == "contraindicated"
    }
    assert contraindicated, "the fixture below would be vacuous"

    for a, b in contraindicated:
        flag = _flag_for(a, b)
        assert flag is not None and flag.is_hard_block, (a, b)

    blocking = {
        (r["drug_a_reference_id"], r["drug_b_reference_id"])
        for r in INTERACTIONS
        if has_hard_block(
            check_interactions(
                _drug(r["drug_a_reference_id"]),
                SafetyContext(
                    current_meds=[_drug(r["drug_b_reference_id"])], interaction_rules=_rules()
                ),
            )
        )
    }
    assert blocking == contraindicated


def test_the_flag_repeats_the_curated_tier_verbatim_for_the_audit_trail():
    """The mapped severity is a display decision; ``details["severity"]`` is what the rule
    actually said, and the immutable suggestion record needs the latter."""
    flag = _flag_for("ASP-75", "WARF-5")
    assert flag is not None
    assert flag.details["severity"] == "major"
    assert flag.summary.startswith("Major interaction")


# --- The curated table underneath it -------------------------------------------------------------


def _partners_by_molecule() -> dict[str, dict[str, str]]:
    """generic name -> {partner generic name: severity}, for each seeded reference id."""
    by_id: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for rule in INTERACTIONS:
        a, b = rule["drug_a_reference_id"], rule["drug_b_reference_id"]
        by_id[a][_BY_REFERENCE_ID[b]["generic_name"]] = rule["severity"]
        by_id[b][_BY_REFERENCE_ID[a]["generic_name"]] = rule["severity"]
    return by_id


def test_every_strength_of_a_molecule_carries_the_same_interactions():
    """The bug this file exists for.

    An interaction is a fact about a molecule; a reference id is a molecule *at a strength*. So
    curating "aspirin interacts with warfarin" against ASP-75 alone leaves Ecosprin 150 — the
    other extremely common strength of the same tablet — matching no rule and returning a clean
    check for a major bleeding risk. Atorvastatin and paracetamol had drifted the same way.

    Asserted per molecule rather than per pair so the failure message names the drug a curator
    has to go and look at.
    """
    by_generic: dict[str, list[str]] = collections.defaultdict(list)
    for row in VOCABULARY:
        by_generic[row["generic_name"]].append(row["reference_id"])

    partners = _partners_by_molecule()
    mismatched: dict[str, dict[str, dict[str, str]]] = {}
    for generic, reference_ids in by_generic.items():
        if len(reference_ids) < 2:
            continue
        seen = {ref: partners.get(ref, {}) for ref in sorted(reference_ids)}
        if len({tuple(sorted(p.items())) for p in seen.values()}) > 1:
            mismatched[generic] = seen

    assert mismatched == {}


def test_every_curated_severity_is_one_the_database_will_accept():
    """``drug_interactions`` carries a CHECK constraint on this column, so a seed row outside the
    enum is a failed migration on a fresh deployment rather than a bad flag."""
    unknown = {r["severity"] for r in INTERACTIONS} - set(CURATED_SEVERITIES)
    assert unknown == set()


def test_every_curated_severity_is_one_the_engine_grades():
    """The engine's fallback exists for robustness, not as a landing place for seed data: a
    curated row that reaches it renders as a generic warning whatever tier the curator meant."""
    from app.core.safety import _INTERACTION_SEVERITY_MAP

    ungraded = {r["severity"] for r in INTERACTIONS} - set(_INTERACTION_SEVERITY_MAP)
    assert ungraded == set()


def test_every_interaction_names_two_real_vocabulary_rows():
    """A rule keyed on a reference id no vocabulary row carries can never fire, and is
    indistinguishable on the screen from a pair that is safe."""
    dangling = {
        (r["drug_a_reference_id"], r["drug_b_reference_id"])
        for r in INTERACTIONS
        if r["drug_a_reference_id"] not in _BY_REFERENCE_ID
        or r["drug_b_reference_id"] not in _BY_REFERENCE_ID
    }
    assert dangling == set()


def test_every_pair_is_stored_in_the_order_the_lookup_expects():
    """``_interaction_key`` sorts a pair alphabetically before looking it up, and
    ``uq_drug_interactions_pair`` is on the stored column order. A row stored the other way round
    is both unreachable and a duplicate the unique constraint will not catch."""
    misordered = [
        (r["drug_a_reference_id"], r["drug_b_reference_id"])
        for r in INTERACTIONS
        if r["drug_a_reference_id"] >= r["drug_b_reference_id"]
    ]
    assert misordered == []


def test_no_pair_is_curated_twice():
    pairs = [(r["drug_a_reference_id"], r["drug_b_reference_id"]) for r in INTERACTIONS]
    duplicated = [pair for pair, count in collections.Counter(pairs).items() if count > 1]
    assert duplicated == []


def test_a_drug_is_never_curated_as_interacting_with_itself():
    assert [r for r in INTERACTIONS if r["drug_a_reference_id"] == r["drug_b_reference_id"]] == []


# --- The specific pairs the grading is judged by -------------------------------------------------


@pytest.mark.parametrize(
    "proposed,current,expected",
    [
        # Both aspirin strengths, which is the regression this file was written for.
        ("ASP-75", "WARF-5", "critical"),
        ("ASP-150", "WARF-5", "critical"),
        ("ASP-75", "DIC-50", "critical"),
        ("ASP-150", "DIC-50", "critical"),
        # Cotrimoxazole inhibits CYP2C9 and displaces warfarin from protein binding — one of the
        # sharpest antibiotic/anticoagulant interactions there is, and it was absent entirely
        # while the same antibiotic's methotrexate interaction was curated as major.
        ("TMP-SMX-960", "WARF-5", "critical"),
        # Both statin strengths.
        ("ATV-10", "AZI-500", "warning"),
        ("ATV-20", "AZI-500", "warning"),
        # Both paracetamol strengths, including the 650 that is the most prescribed of the two.
        ("PCM-500", "AMX-CLV-625", "info"),
        ("PCM-650", "AMX-CLV-625", "info"),
    ],
)
def test_a_named_pair_carries_the_weight_a_clinician_would_expect(proposed, current, expected):
    flag = _flag_for(proposed, current)
    assert flag is not None, f"{proposed} + {current} produced no interaction flag at all"
    assert flag.severity == expected


def test_an_anticoagulant_and_an_antiplatelet_is_never_merely_informational():
    """A blanket statement about the class of pair most likely to bleed a patient.

    Any curated rule between warfarin and an antiplatelet or an NSAID has to carry real weight;
    an "info" flag here is a blue note beside a GI-bleed risk.
    """
    bleeding_classes = {"Antiplatelet", "NSAID"}
    checked = 0
    for rule in INTERACTIONS:
        ids = (rule["drug_a_reference_id"], rule["drug_b_reference_id"])
        if "WARF-5" not in ids:
            continue
        other = ids[0] if ids[1] == "WARF-5" else ids[1]
        if _BY_REFERENCE_ID[other].get("drug_class") not in bleeding_classes:
            continue
        checked += 1
        assert rule["severity"] == "major", (other, rule["severity"])

    assert checked >= 4, "expected the aspirin and NSAID pairs to be curated"


# --- More than one curated rule for the same pair -------------------------------------------------
#
# ``uq_drug_interactions_pair`` is on the ordered columns, so nothing in the schema stops the same
# pair being curated twice — ``(ASP-75, WARF-5)`` and ``(WARF-5, ASP-75)`` are two rows the
# database accepts, and they are the same interaction. The engine used to key its rule lookup with
# a dict comprehension, which kept whichever of them the query returned last and silently dropped
# the rest. With a ``contraindicated`` row and a ``minor`` row for one pair, this module's
# canonical hard block came back as an informational note because of row order.


def _reversed_pair_rules() -> list[InteractionRule]:
    """One pair, curated twice in opposite column orders, disagreeing about severity."""
    return [
        InteractionRule("ASP-75", "WARF-5", "contraindicated", "absolute per source A"),
        InteractionRule("WARF-5", "ASP-75", "minor", "watch per source B"),
    ]


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_the_hard_block_survives_whichever_curated_row_is_read_last(order):
    """The defect, stated as the property that fixes it: the answer cannot depend on row order.

    Both orderings are tested because that is exactly what varied — nothing constrains the order
    the rule table comes back in, so a chart could grade the same pair differently between two
    reads with no change to the data underneath.
    """
    rules = _reversed_pair_rules()
    ctx = SafetyContext(
        current_meds=[_drug("ASP-75")],
        interaction_rules=[rules[order[0]], rules[order[1]]],
    )

    flags = check_interactions(_drug("WARF-5"), ctx)

    assert has_hard_block(flags), "the contraindicated rule was lost to row order"
    assert [f.severity for f in flags] == ["hard_block", "info"], (
        "every curated rule for the pair is reported, most serious first"
    )


def test_a_pair_curated_twice_in_the_same_words_is_reported_once():
    """The other half: a duplicate is one fact entered twice, not two facts.

    This is the shape a reversed-pair duplicate normally takes — the same statement, at the same
    severity, with the two ids swapped — and putting the identical sentence on the card twice is
    noise on a screen whose value is that it is short enough to read.
    """
    ctx = SafetyContext(
        current_meds=[_drug("ASP-75")],
        interaction_rules=[
            InteractionRule("ASP-75", "WARF-5", "contraindicated", "Absolute contraindication"),
            InteractionRule("WARF-5", "ASP-75", "contraindicated", "absolute contraindication "),
        ],
    )

    flags = check_interactions(_drug("WARF-5"), ctx)

    assert len(flags) == 1
    assert flags[0].is_hard_block is True


def test_two_genuinely_different_statements_about_one_pair_are_both_shown():
    """Two sources describing two mechanisms are two clinical facts, and a clinician told only
    the milder of them has been told the wrong thing."""
    ctx = SafetyContext(
        current_meds=[_drug("ASP-75")],
        interaction_rules=[
            InteractionRule("ASP-75", "WARF-5", "major", "additive antiplatelet effect"),
            InteractionRule("WARF-5", "ASP-75", "moderate", "protein-binding displacement"),
        ],
    )

    flags = check_interactions(_drug("WARF-5"), ctx)

    assert [f.severity for f in flags] == ["critical", "warning"]
    assert {f.details["management"] for f in flags} == {None}
    assert len({f.summary for f in flags}) == 2


def test_every_rule_for_a_pair_keeps_its_own_id_so_each_can_be_overridden():
    """Each flag has to carry the id of the rule it came from.

    A hard block is cleared only by an override recorded against the check it produced (Critical
    Safety Rule #3). Two rules collapsed into one flag, or two flags sharing one rule id, would
    make "which finding did the clinician document their reasoning against" unanswerable months
    later — which is the whole point of the override record.
    """
    ctx = SafetyContext(
        current_meds=[_drug("ASP-75")],
        interaction_rules=[
            InteractionRule("ASP-75", "WARF-5", "contraindicated", "source A", interaction_id="a"),
            InteractionRule("WARF-5", "ASP-75", "major", "source B", interaction_id="b"),
        ],
    )

    flags = check_interactions(_drug("WARF-5"), ctx)

    assert [f.drug_interaction_id for f in flags] == ["a", "b"]


def test_several_interacting_medications_all_surface_not_just_the_first():
    """Every current medication the proposal meets is reported, not the worst or the first one.

    A patient on warfarin, an NSAID and a macrolide is the ordinary polypharmacy case, and a
    screen that names one of three interacting drugs reads as a complete answer.
    """
    ctx = SafetyContext(
        current_meds=[_drug("WARF-5"), _drug("DIC-50"), _drug("AZI-500")],
        interaction_rules=_rules(),
    )

    flags = check_interactions(_drug("ASP-75"), ctx)

    interacting = {f.details["interacting_drug"] for f in flags}
    assert {"Warfarin", "Diclofenac"} <= interacting
    assert len(flags) >= 2
