"""Contraindication matching against how Indian charts are actually written.

R44 widened condition matching from string equality, because a chart saying "Asthma" — the
ordinary way it is written — did not hard-block atenolol while "Bronchial Asthma" did. This file
is the adversarial pass over that widening: OCR artefacts, ward shorthand, obstetric notation,
British spellings, and problem lists in a script the tokeniser cannot read at all.

Two kinds of assertion here, and both matter.

The positives pin wordings that must reach the rule. Each one was found by running real chart
phrasings through the matcher and watching them return nothing — "Haematemesis" against
warfarin's absolute bleeding block, "UGI bleed" against the same, "Acid Peptic Disease" against
the NSAID rules.

The negatives pin the boundary. Negation and hedging must NOT block; and the deliberate refusals
— fuzzy matching, ambiguous two-letter abbreviations, obstetric formulae — are recorded as tests
so the next person to widen the matcher knows they are choices and not oversights.
"""

from __future__ import annotations

import pytest

from app.core.safety import (
    ContraindicationRule,
    DrugRef,
    PatientCondition,
    SafetyContext,
    check_contraindications,
    check_unevaluated_conditions,
)

pytestmark = pytest.mark.asyncio

# The seeded absolute blocks these wordings have to reach.
_RULES = {
    "asthma": (
        DrugRef("ATE-50", "Atenolol", "Beta-blocker"),
        ContraindicationRule(
            "ATE-50", "Bronchial Asthma", "absolute", "Beta-blockade in asthma", True
        ),
    ),
    "ulcer": (
        DrugRef("IBU-400", "Ibuprofen", "NSAID"),
        ContraindicationRule(
            "IBU-400", "Peptic Ulcer Disease", "absolute", "NSAID in peptic ulcer", True
        ),
    ),
    "pregnancy": (
        DrugRef("RAM-5", "Ramipril", "ACE Inhibitor"),
        ContraindicationRule(
            "RAM-5", "Pregnancy", "absolute", "ACE inhibitors are fetotoxic", True
        ),
    ),
    "bleeding": (
        DrugRef("WARF-5", "Warfarin", "Vitamin K antagonist"),
        ContraindicationRule(
            "WARF-5", "Active Bleeding", "absolute", "Anticoagulation in active bleeding", True
        ),
    ),
}


def _outcome(rule_key: str, charted: str, icd10: str | None = None) -> str:
    """ "block", "warn", or "silent" — what a clinician would actually see."""
    drug, rule = _RULES[rule_key]
    flags = check_contraindications(
        drug,
        SafetyContext(
            contraindication_rules=[rule],
            conditions=[PatientCondition(condition_name=charted, icd10_code=icd10)],
        ),
    )
    if not flags:
        return "silent"
    return "block" if flags[0].is_hard_block else "warn"


# --- OCR artefacts --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "charted",
    [
        "BRONCHIAL ASTHMA",
        "bronchial asthma",
        "Bronchial  Asthma",  # doubled space
        "Bronchial-Asthma",
        "Bronchial_Asthma",
        "Asthma.",
        "Asthma,",
        "Asthma;",
        "* Asthma",
        "1. Asthma",
        "- Asthma",
        "Asthma\n",
        "\tAsthma ",
        "Bronchia1 Asthma",  # OCR reads l as 1; the digit is stripped as punctuation
        "Bronchial Asthma (moderate persistent)",
        "Asthma [severe]",
    ],
)
async def test_layout_and_punctuation_noise_does_not_defeat_a_hard_block(charted: str) -> None:
    """Everything a scanner, a bullet list or a stray keystroke adds to a condition name.
    Each of these is the same diagnosis, and the block is non-overridable — CLAUDE.md rule #3."""
    assert _outcome("asthma", charted) == "block"


async def test_a_condition_split_by_an_ocr_substitution_inside_a_word_is_not_matched() -> None:
    """The deliberate boundary. "Asthrna" is "Asthma" with m read as rn, and nothing here will
    catch it: the matcher is built from exact token rewrites precisely so that it cannot pair two
    different conditions by resemblance. Fuzzy-matching condition names would buy this case at
    the price of a hard block fired by a coincidence, which is a worse trade on a
    non-overridable rule. Recorded as a test so the next widening knows it is a choice."""
    assert _outcome("asthma", "Bronchial Asthrna") == "silent"


# --- ward shorthand -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "charted",
    ["K/C/O Bronchial Asthma", "k/c/o asthma", "Known case of asthma", "C/O Asthma"],
)
async def test_known_case_of_is_an_assertion_not_a_hedge(charted: str) -> None:
    """ "K/C/O" is the commonest way an Indian chart states an established diagnosis. Reading it
    as uncertainty would downgrade most of the problem list to warnings."""
    assert _outcome("asthma", charted) == "block"


@pytest.mark.parametrize("charted", ["PUD", "Gastric Ulcer", "Duodenal Ulcer", "peptic ulcer"])
async def test_ulcer_synonyms_reach_the_nsaid_block(charted: str) -> None:
    assert _outcome("ulcer", charted) == "block"


@pytest.mark.parametrize("charted", ["APD", "Acid Peptic Disease", "acid peptic disease"])
async def test_acid_peptic_disease_warns_rather_than_blocks(charted: str) -> None:
    """The Indian umbrella term, and it covers gastritis and reflux as well as ulceration — so
    the chart is less specific than the rule, and the asymmetry R44 built says warn rather than
    assert a diagnosis the chart does not make. It matched nothing at all before this."""
    assert _outcome("ulcer", charted) == "warn"


@pytest.mark.parametrize(
    "charted",
    [
        "Primi",
        "Primigravida",
        "Multigravida",
        "Antenatal",
        "20 weeks pregnant",
        "Intrauterine Pregnancy",
        "Pregnancy test positive",
    ],
)
async def test_pregnancy_shorthand_reaches_the_ace_inhibitor_block(charted: str) -> None:
    """The highest-stakes block in the seed data — ACE inhibitors are fetotoxic — against the
    wordings an antenatal chart actually uses. "Primi" and "Antenatal" were silent."""
    assert _outcome("pregnancy", charted) == "block"


async def test_an_obstetric_formula_is_deliberately_not_parsed() -> None:
    """ "G2P1L1" is gravida 2, para 1, living 1 — which is a woman's obstetric *history* and does
    not by itself say she is pregnant now. Parsing it into a pregnancy hard block would assert
    something the notation does not, on the one rule that cannot be dismissed."""
    assert _outcome("pregnancy", "G2P1L1") == "silent"


@pytest.mark.parametrize(
    "charted",
    [
        "Malena",
        "Malaena",
        "Melena",
        "Haematemesis",
        "Hematemesis",
        "Hematochezia",
        "UGI bleed",
        "Upper GI Bleed",
        "GI bleeding",
        "PR bleed",
        "Bleeding P/R",
        "Per rectal bleeding",
        "Active Haemorrhage",
    ],
)
async def test_bleeding_named_by_its_site_reaches_the_warfarin_block(charted: str) -> None:
    """Each of these IS active bleeding by definition, so the block stands rather than softening
    to a warning. Six of them were silent: the existing "gi bleed" entry only covered a chart
    that spells GI as its own word, and "UGI bleed" is one token."""
    assert _outcome("bleeding", charted) == "block"


async def test_an_ambiguous_two_letter_abbreviation_is_deliberately_not_matched() -> None:
    """ "BA" is bronchial asthma on a chest ward and half a dozen other things elsewhere. A
    two-letter key cannot be disambiguated from context this module does not have, and the cost
    of being wrong is a non-overridable block on a beta-blocker."""
    assert _outcome("asthma", "BA") == "silent"


# --- negation and hedging must survive all of the above ---------------------------------------


@pytest.mark.parametrize(
    "charted",
    [
        "No h/o asthma",
        "No asthma",
        "Asthma ruled out",
        "Family history of asthma",
        "FH of bronchial asthma",
        "Denies asthma",
        "Negative for asthma",
        "Asthma - not present",
    ],
)
async def test_a_chart_saying_the_patient_does_not_have_it_blocks_nothing(charted: str) -> None:
    """The widening must not have made negation invisible. Every one of these is the chart
    stating the condition is *not* this patient's problem."""
    assert _outcome("asthma", charted) == "silent"


@pytest.mark.parametrize(
    "charted", ["H/O Bronchial Asthma", "? Asthma", "Suspected asthma", "Past asthma", "r/o asthma"]
)
async def test_a_hedged_condition_warns_and_never_blocks(charted: str) -> None:
    """Not grounds for a hard block — the chart does not assert the patient currently has it —
    but very much grounds for telling the clinician the rule exists."""
    assert _outcome("asthma", charted) == "warn"


@pytest.mark.parametrize(
    "charted",
    [
        "Pregnancy — delivered",
        "Ectopic pregnancy — terminated",
        "Pregnancy, aborted",
        "Pregnancy - miscarried",
        "S/P LSCS, pregnancy",
        "Status post delivery — pregnancy",
    ],
)
async def test_a_pregnancy_the_chart_says_has_ended_does_not_block_forever(charted: str) -> None:
    """Found by this file. "Terminated" and "delivered" were not resolution markers, so a line
    left on a problem list hard-blocked ramipril on a postpartum woman — and postpartum
    hypertension is exactly when an ACE inhibitor is correctly prescribed. Hedged rather than
    silent: a chart is not always right about what has ended, so the rule is still named."""
    assert _outcome("pregnancy", charted) == "warn"


@pytest.mark.parametrize(
    "charted",
    [
        "Postpartum haemorrhage",
        "Intracranial haemorrhage",
        "Subarachnoid hemorrhage",
        "Variceal haemorrhage",
    ],
)
async def test_a_haemorrhage_named_by_its_site_reaches_the_block(charted: str) -> None:
    """ "Postpartum" looks like a past-tense qualifier and is not one, so it is deliberately
    absent from the resolution markers — hedging it would downgrade warfarin's absolute block on
    an obstetric emergency to an advisory line. All four of these were silent before: the
    existing entry is "active hemorrhage", and that key is not a subset of "postpartum
    hemorrhage"."""
    assert _outcome("bleeding", charted) == "block"


async def test_the_bare_word_bleeding_is_deliberately_not_the_rule() -> None:
    """The refusal on the other side. Rewriting "bleeding" alone to "active bleeding" would
    reach "post-partum bleeding" — and also "bleeding gums", "contact bleeding" and every
    bleeding *tendency* on a problem list, each of them a non-overridable block on an
    anticoagulant the patient may well need. The site-named terms above are matched because each
    one denotes bleeding that is happening; the bare word does not."""
    assert _outcome("bleeding", "Post-partum bleeding") == "silent"
    assert _outcome("bleeding", "Bleeding gums") == "silent"


# --- non-Latin scripts and unreadable rows ----------------------------------------------------


@pytest.mark.parametrize(
    "charted",
    ["गर्भावस्था", "अस्थमा", "ব্রঙ্কিয়াল অ্যাজমা", "喘息", "‡‡‡", "???", "___", "...", "   "],
)
async def test_a_condition_the_tokeniser_cannot_read_is_reported_not_dropped(charted: str) -> None:
    """The recurring bug in this engine, found in a fifth place. Matching compares [a-z0-9]
    token sets, so a problem list in Devanagari or an OCR blob tokenises to nothing, and a token
    set of nothing satisfies none of the three comparisons — the row falls through and every
    contraindication rule silently has nothing to say about it.

    It still blocks nothing, because translating a condition name is a guess and a guess that
    manufactures a hard block is its own harm. What it does now is say the comparison did not
    happen."""
    ctx = SafetyContext(conditions=[PatientCondition(condition_name=charted)])

    flags = check_unevaluated_conditions(ctx)

    assert [f.check_type for f in flags] == ["unevaluated_condition"]
    assert flags[0].is_hard_block is False
    assert flags[0].details["evaluated"] is False
    assert _outcome("pregnancy", charted) == "silent"


async def test_an_icd10_code_rescues_a_condition_written_in_any_script() -> None:
    """The code path never reads the name, so a coded condition is evaluated whatever script it
    is in — and must not then be reported as unevaluated. This is the answer the flag points
    the clinician at."""
    drug, rule = _RULES["pregnancy"]
    coded_rule = ContraindicationRule(
        rule.drug_reference_id,
        rule.condition_name,
        rule.severity,
        rule.description,
        rule.is_absolute,
        icd10_code="Z34",
    )
    condition = PatientCondition(condition_name="गर्भावस्था", icd10_code="Z34.90")

    flags = check_contraindications(
        drug, SafetyContext(contraindication_rules=[coded_rule], conditions=[condition])
    )

    assert [f.is_hard_block for f in flags] == [True]
    assert check_unevaluated_conditions(SafetyContext(conditions=[condition])) == []


async def test_a_readable_condition_that_simply_matches_no_rule_is_not_reported() -> None:
    """Otherwise every chart would carry the flag: "no rule bears on this condition" is the
    normal case, and only "this row could not be compared to any rule at all" is the failure."""
    ctx = SafetyContext(conditions=[PatientCondition(condition_name="Seasonal allergic rhinitis")])

    assert check_unevaluated_conditions(ctx) == []


async def test_every_unreadable_row_is_named_once_for_the_chart() -> None:
    ctx = SafetyContext(
        conditions=[
            PatientCondition(condition_name="गर्भावस्था"),
            PatientCondition(condition_name="‡‡‡"),
            PatientCondition(condition_name="गर्भावस्था"),
            PatientCondition(condition_name="Bronchial Asthma"),
        ]
    )

    flags = check_unevaluated_conditions(ctx)

    assert len(flags) == 1
    assert sorted(flags[0].details["unevaluated_conditions"]) == sorted(["गर्भावस्था", "‡‡‡"])
    assert "Bronchial Asthma" not in flags[0].summary


async def test_a_blank_condition_row_is_counted_even_though_it_cannot_be_quoted() -> None:
    """A row with no name is still a row nothing was checked against."""
    flags = check_unevaluated_conditions(
        SafetyContext(conditions=[PatientCondition(condition_name="   ")])
    )

    assert flags[0].details["unevaluated_condition_count"] == 1
    assert flags[0].details["unevaluated_conditions"] == []


async def test_a_chart_with_no_conditions_says_nothing() -> None:
    assert check_unevaluated_conditions(SafetyContext()) == []


# --- the widening must not reach across conditions ---------------------------------------------


@pytest.mark.parametrize(
    ("rule_key", "charted"),
    [
        ("asthma", "Bronchial carcinoma"),
        ("asthma", "Chronic bronchitis"),
        ("ulcer", "Ulcerative colitis"),
        ("ulcer", "Diabetic foot ulcer"),
        ("ulcer", "Corneal ulcer"),
        ("pregnancy", "Ectopic pregnancy — terminated"),
        ("bleeding", "Bleeding disorder — von Willebrand"),
        ("bleeding", "Nose bleed last year"),
    ],
)
async def test_a_different_condition_that_shares_words_does_not_fire_the_rule(
    rule_key: str, charted: str
) -> None:
    """The risk any widening carries: matching two different conditions because they resemble
    one another. None of these may produce a hard block — a warning naming the rule is the most
    an overlapping wording may earn."""
    assert _outcome(rule_key, charted) != "block"


async def test_the_strongest_match_on_a_chart_wins_over_a_hedged_one() -> None:
    """A chart carrying both "h/o asthma" and "Asthma" blocks rather than warns, whichever order
    the rows come back in."""
    drug, rule = _RULES["asthma"]
    rows = [
        PatientCondition(condition_name="H/O Bronchial Asthma"),
        PatientCondition(condition_name="Bronchial Asthma"),
    ]
    for conditions in (rows, list(reversed(rows))):
        flags = check_contraindications(
            drug, SafetyContext(contraindication_rules=[rule], conditions=conditions)
        )
        assert [f.is_hard_block for f in flags] == [True], conditions


async def test_the_matcher_is_stable_under_a_shuffled_problem_list() -> None:
    """Rules run over the whole list, so the answer must not depend on where in it the matching
    row happens to sit."""
    drug, rule = _RULES["bleeding"]
    noise = [PatientCondition(condition_name=f"Unrelated finding {i}") for i in range(5)]
    target = PatientCondition(condition_name="Haematemesis")
    for position in range(len(noise) + 1):
        conditions = noise[:position] + [target] + noise[position:]
        flags = check_contraindications(
            drug, SafetyContext(contraindication_rules=[rule], conditions=conditions)
        )
        assert [f.is_hard_block for f in flags] == [True], position
