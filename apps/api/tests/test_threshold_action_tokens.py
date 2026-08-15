"""The renal and hepatic hard blocks turned on the casing of a word inside a JSON column.

``_evaluate_renal`` and ``_evaluate_hepatic`` decide whether a breached threshold is a hard block
with a single comparison::

    is_hard = action == "contraindicated"

``action`` is a free-text key inside ``contraindications.renal_threshold`` /
``hepatic_threshold``, which are untyped JSON columns. Unlike ``contraindications.severity`` and
``drug_interactions.severity`` — both of which have a CHECK constraint keeping unreadable values
out of the table — nothing validates what goes in there. ``data/drugs/schema.json`` declares
``action`` as a bare string, and ``app.db.seed`` does not validate against that schema in any
case: it inserts whatever the JSON file says.

So a curated rule written ``"action": "Contraindicated"`` loaded without complaint, and the
metformin-below-eGFR-30 rule — this engine's canonical hard block, the one CLAUDE.md Rule #3 is
usually explained with — came back as a dismissible warning. Same for ``"CONTRA-INDICATED"``,
which is how a second data source is as likely to spell it, and for a trailing space left by a
spreadsheet export.

That is the R44 defect ("a contraindication hard block defeated by condition wording") arriving
through the other half of the rule: not what the chart calls the condition, but what the curator
called the action.

The fix is ``app.core.safety._token``, applied at every point this module reads a curated
enumerated value. The severity columns go through it too — redundantly, since the database
already refuses what they cannot read, but the interaction-severity test explicitly contemplates
that constraint being relaxed, and a vocabulary should be a property of the value rather than of
how it was typed.
"""

from __future__ import annotations

import pytest

from app.core.safety import (
    ContraindicationRule,
    DrugRef,
    HepaticPanel,
    InteractionRule,
    PatientCondition,
    SafetyContext,
    check_contraindications,
    check_interactions,
    has_hard_block,
)

METFORMIN = DrugRef("MET-500", "Metformin", drug_class="Biguanide")
ASPIRIN = DrugRef("ASP-75", "Aspirin", drug_class="Antiplatelet")
WARFARIN = DrugRef("WARF-5", "Warfarin", drug_class="Anticoagulant")


def _renal_rule(action: object) -> ContraindicationRule:
    """The shipped metformin rule, with only the curated action's spelling varied."""
    return ContraindicationRule(
        drug_reference_id="MET-500",
        condition_name="Severe renal impairment",
        severity="absolute",
        description="Metformin is contraindicated below an eGFR of 30 (lactic acidosis risk).",
        is_absolute=True,
        renal_threshold={"egfr_below": 30, "action": action},
        contraindication_id="c-renal",
    )


# Every spelling of the same curated instruction that a real data source produces: the canonical
# one, a title-cased export, an upper-cased one, the hyphenated compound, the space-separated
# compound, and one with the whitespace a spreadsheet round-trip leaves behind.
@pytest.mark.parametrize(
    "action",
    [
        "contraindicated",
        "Contraindicated",
        "CONTRAINDICATED",
        "contra-indicated",
        "CONTRA-INDICATED",
        "contra indicated",
        "  contraindicated  ",
        "contra_indicated",
    ],
)
def test_a_renal_threshold_hard_blocks_however_the_action_is_spelled(action: str) -> None:
    ctx = SafetyContext(egfr=20.0, contraindication_rules=[_renal_rule(action)])

    flags = check_contraindications(METFORMIN, ctx)

    assert len(flags) == 1
    assert flags[0].check_type == "renal_dose"
    assert flags[0].is_hard_block is True, f"{action!r} lost the hard block"
    assert flags[0].severity == "hard_block"
    assert has_hard_block(flags) is True


def test_the_summary_still_quotes_the_action_as_curated() -> None:
    """Normalisation is for the comparison, not for what the clinician is shown.

    The flag names the recommended action, and what the reference data actually says is the thing
    a clinician can look up and a curator can search for. Rewriting it to this module's
    normalised form would make the screen disagree with the table it came from.
    """
    ctx = SafetyContext(egfr=20.0, contraindication_rules=[_renal_rule("CONTRA-INDICATED")])

    flag = check_contraindications(METFORMIN, ctx)[0]

    assert "CONTRA-INDICATED" in flag.summary
    assert flag.details["action"] == "CONTRA-INDICATED"


def test_an_action_that_is_not_a_block_stays_a_warning() -> None:
    """The normalisation must not turn every threshold into a block.

    ``reduce_dose_50pct`` is a real curated action on the shipped digoxin and metformin rules, and
    it is exactly what a dose-adjustment band is for. It has to keep coming back dismissible.
    """
    ctx = SafetyContext(egfr=20.0, contraindication_rules=[_renal_rule("Reduce_Dose_50pct")])

    flag = check_contraindications(METFORMIN, ctx)[0]

    assert flag.is_hard_block is False
    assert flag.severity == "warning"


@pytest.mark.parametrize("action", [None, 42, {"do": "stop"}, ["contraindicated"]])
def test_an_action_that_is_not_a_string_is_read_as_no_action_rather_than_raising(
    action: object,
) -> None:
    """This module is Rule #8 code: it runs offline and must not be able to fail.

    A JSON column can hold anything, and a curator can leave a null or a number in ``action``.
    Comparing that against a literal is harmless, but so is any careless ``.lower()`` added later
    — so the normaliser answers "" for a non-string, which matches no literal and takes the
    ordinary non-blocking path. The threshold is still breached, so the clinician still gets the
    flag; what is absent is a claim about the action that the data does not support.
    """
    ctx = SafetyContext(egfr=20.0, contraindication_rules=[_renal_rule(action)])

    flag = check_contraindications(METFORMIN, ctx)[0]

    assert flag.is_hard_block is False
    assert flag.details["egfr_threshold"] == 30


def test_an_explicit_null_action_is_described_as_review_not_as_none() -> None:
    """``threshold.get("action", "review")`` only defaults on an *absent* key.

    A curated rule carrying ``"action": null`` — which is what a JSON export of an empty
    spreadsheet cell produces — returned None from that form and printed "Recommended action:
    None." into text a clinician reads.
    """
    ctx = SafetyContext(egfr=20.0, contraindication_rules=[_renal_rule(None)])

    flag = check_contraindications(METFORMIN, ctx)[0]

    assert "None" not in flag.summary
    assert flag.details["action"] == "review"


@pytest.mark.parametrize("action", ["contraindicated", "Contraindicated", "CONTRA-INDICATED"])
def test_a_hepatic_threshold_hard_blocks_however_the_action_is_spelled(action: str) -> None:
    """The hepatic evaluator carries the same comparison, and the shipped corpus uses it."""
    rule = ContraindicationRule(
        drug_reference_id="MTX-7.5",
        condition_name="Hepatic impairment",
        severity="absolute",
        description="Methotrexate is contraindicated in significant hepatic impairment.",
        is_absolute=True,
        hepatic_threshold={"bilirubin_above": 3.0, "action": action},
        contraindication_id="c-hep",
    )
    ctx = SafetyContext(hepatic=HepaticPanel(bilirubin_mg_dl=5.2), contraindication_rules=[rule])

    flags = check_contraindications(DrugRef("MTX-7.5", "Methotrexate"), ctx)

    assert len(flags) == 1
    assert flags[0].check_type == "hepatic_dose"
    assert flags[0].is_hard_block is True, f"{action!r} lost the hard block"


def test_the_block_reaches_a_combination_product_through_its_component() -> None:
    """The two fixes have to compose: the R47 ingredient walk, and this one.

    Glycomet GP is the product an Indian prescription actually names, the metformin rule is keyed
    on MET-500, and the curated action is spelled with a capital. Both indirections have to hold
    at once or the block is gone.
    """
    glycomet_gp = DrugRef(
        "MET-GLM-1-500",
        "Metformin + Glimepiride",
        drug_class="Biguanide + Sulfonylurea",
        components=(METFORMIN, DrugRef("GLM-1", "Glimepiride", drug_class="Sulfonylurea")),
    )
    ctx = SafetyContext(egfr=18.0, contraindication_rules=[_renal_rule("Contraindicated")])

    flags = check_contraindications(glycomet_gp, ctx)

    assert has_hard_block(flags) is True
    assert "Metformin" in flags[0].summary


@pytest.mark.parametrize("severity", ["absolute", "Absolute", "ABSOLUTE"])
def test_a_condition_contraindication_hard_blocks_however_the_severity_is_cased(
    severity: str,
) -> None:
    """Defence in depth: ``ck_contraindications_severity`` keeps these out of the table today.

    ``is_absolute`` is a separate boolean and is the primary signal, so this is the path taken by
    a row whose severity says absolute and whose boolean was not set — a disagreement inside one
    row, which Rule #2 resolves toward the more conservative reading.
    """
    rule = ContraindicationRule(
        drug_reference_id="ATN-50",
        condition_name="Asthma",
        severity=severity,
        description="Beta-blockers can precipitate bronchospasm.",
        is_absolute=False,
        contraindication_id="c-asthma",
    )
    ctx = SafetyContext(conditions=[PatientCondition("Asthma")], contraindication_rules=[rule])

    flags = check_contraindications(DrugRef("ATN-50", "Atenolol"), ctx)

    assert len(flags) == 1
    assert flags[0].is_hard_block is True


@pytest.mark.parametrize(
    ("severity", "expected", "hard"),
    [
        ("contraindicated", "hard_block", True),
        ("Contraindicated", "hard_block", True),
        ("CONTRA-INDICATED", "hard_block", True),
        ("Major", "critical", False),
        ("MINOR", "info", False),
    ],
)
def test_interaction_bands_are_read_case_insensitively(
    severity: str, expected: str, hard: bool
) -> None:
    """Also defence in depth — ``ck_drug_interactions_severity`` guards the column today.

    Pinned because the interaction-severity suite states the unknown-value behaviour is "what
    happens if that constraint is ever relaxed", and a relaxed constraint is exactly when a
    second source's ``"Major"`` arrives.
    """
    ctx = SafetyContext(
        current_meds=[WARFARIN],
        interaction_rules=[InteractionRule("ASP-75", "WARF-5", severity, "Bleeding risk.")],
    )

    flags = check_interactions(ASPIRIN, ctx)

    assert len(flags) == 1
    assert flags[0].severity == expected
    assert flags[0].is_hard_block is hard
