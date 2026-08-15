"""Critical Safety Rule #4 is enforced on model output, not only requested in the prompt.

Every agent prompt carries ``_SAFETY_FRAMING``, which asks the model never to write "the patient
has X" or "give drug Y", and ``regulatory_service`` published that paragraph as the *control*
for the rule. A prompt is a request, not a control. The model answering it is whichever provider
is configured at deployment time — in production, often a small free one — and the failure is
silent, because "Give aspirin 300 mg" is fluent, correctly formatted, schema-conforming text
that satisfies every other check between the provider socket and the clinician's screen.

So the enforcement is deterministic, offline (Rule #8 requires that of anything the
deterministic path leans on) and applied at the one place model-written text becomes a
``ClinicalSuggestion``.

The tests split three ways, and the middle group is the one that would be missed:

* **It fires** on the forms CLAUDE.md names as bugs.
* **It does not fire** on text that was already prescriber-framed, and it never rewrites a
  hedge into a second hedge. A guard that mangles correct output is worse than no guard: it
  would make the whole pipeline's language less trustworthy, not more.
* **It preserves the clinical content.** The drug, the dose and the diagnosis survive every
  rewrite verbatim. This module changes the modality of a sentence and nothing else — a rewrite
  that lost a dose would be a far worse defect than the one being fixed.
"""

from __future__ import annotations

import pytest

from app.agents.state import CaseState, Evidence, Hypothesis, ManagementOption
from app.agents.synthesis import build_suggestions
from app.core.clinical_language import has_certainty_language, prescriber_framed

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------- it fires
# The four patterns CLAUDE.md's "Common Pitfalls" §3 says to grep for, plus the forms a model
# actually reaches for when it ignores the framing instruction.
FORBIDDEN = [
    ("Give aspirin 300 mg orally.", "aspirin 300 mg"),
    ("Administer IV ceftriaxone 1 g.", "ceftriaxone 1 g"),
    ("Prescribe metformin 500 mg BD.", "metformin 500 mg"),
    ("Dispense amoxicillin 500 mg TDS.", "amoxicillin 500 mg"),
    ("Initiate ramipril 2.5 mg OD.", "ramipril 2.5 mg"),
    ("Start the patient on insulin glargine 10 units.", "insulin glargine 10 units"),
    ("Stop warfarin before the procedure.", "warfarin"),
    ("The patient has bacterial pneumonia.", "bacterial pneumonia"),
    ("Patient is suffering from acute pancreatitis.", "acute pancreatitis"),
    ("Diagnose with type 2 diabetes mellitus.", "type 2 diabetes mellitus"),
    ("Diagnosis is acute coronary syndrome.", "acute coronary syndrome"),
    ("This is definitely a STEMI.", "STEMI"),
    ("It is certainly viral hepatitis.", "viral hepatitis"),
]


@pytest.mark.parametrize(("text", "content"), FORBIDDEN)
async def test_forbidden_phrasing_is_rewritten(text, content):
    rewritten, changed = prescriber_framed(text)

    assert changed is True, f"{text!r} was left as it stood"
    assert has_certainty_language(text) is True
    # The whole point: the modality changed, the clinical substance did not.
    assert content in rewritten, f"the rewrite of {text!r} lost {content!r}"
    assert not has_certainty_language(rewritten), f"{rewritten!r} still asserts or commands"


async def test_a_directive_in_a_later_sentence_is_caught_too():
    """Models hedge the opening line and then give an order in the next one."""
    rewritten, changed = prescriber_framed(
        "Evidence suggests a lower respiratory infection. Give amoxicillin 500 mg TDS."
    )

    assert changed is True
    assert "Guidelines support considering amoxicillin 500 mg TDS." in rewritten


# ----------------------------------------------------------- it does not fire
ALREADY_FRAMED = [
    "Guidelines support considering aspirin 300 mg.",
    "Findings are consistent with bacterial pneumonia.",
    "Evidence suggests considering an ECG before discharge.",
    "Consider evaluating for pulmonary embolism.",
    "ICMR STW for acute coronary syndrome supports considering dual antiplatelet therapy.",
    # A hedge that happens to contain a directive verb mid-clause. The anchor exists for this:
    # rewriting here would nest one hedge inside another and read as machine-mangled text.
    "Evidence suggests we should give aspirin only after bleeding risk is assessed.",
    "There is no indication to start antibiotics at this stage.",
    "",
]


@pytest.mark.parametrize("text", ALREADY_FRAMED)
async def test_compliant_text_is_left_exactly_as_it_stands(text):
    rewritten, changed = prescriber_framed(text)

    assert changed is False, f"{text!r} was rewritten when it was already compliant"
    assert rewritten == text


async def test_the_rewrite_is_idempotent():
    """Safe to apply at more than one layer as the pipeline grows."""
    once, _ = prescriber_framed("Give aspirin 300 mg. The patient has unstable angina.")
    twice, changed = prescriber_framed(once)

    assert twice == once
    assert changed is False


async def test_deterministic_safety_text_is_not_softened():
    """A hard block is the one place a firm statement is correct, and it must survive.

    ``app.core.safety`` writes these offline and they are already prescriber-framed, so nothing
    here should match them. The check matters because softening a contraindication block would
    turn Rule #3's hard block into a suggestion at the last moment before display.
    """
    block = (
        "Amoxicillin conflicts with a documented penicillin allergy on this chart. "
        "This suggestion is blocked and requires documented clinician reasoning to override."
    )
    rewritten, changed = prescriber_framed(block)

    assert changed is False
    assert rewritten == block


# ------------------------------------------------------- wired into synthesis
def _case() -> CaseState:
    return CaseState(patient_id="p1", presenting_complaint="Cough and fever for three days")


def _state_with_hypothesis(**overrides) -> CaseState:
    state = _case()
    hypothesis = Hypothesis(
        diagnosis_name=overrides.get("diagnosis_name", "Community-acquired pneumonia"),
        rationale=overrides.get("rationale", "Give amoxicillin 500 mg TDS for five days."),
        probability_band="moderate",
        source_agent="hypothesis_panel",
    )
    state.hypothesis_set.append(hypothesis)
    return state


async def test_a_suggestion_body_never_leaves_synthesis_imperative():
    """The chokepoint. Every clinical string the engine emits is built in one function."""
    suggestions = build_suggestions(_state_with_hypothesis())

    body = suggestions[0]["body"]
    assert body.startswith("Guidelines support considering amoxicillin 500 mg TDS")
    assert not has_certainty_language(body)


async def test_a_suggestion_title_never_leaves_synthesis_certain():
    suggestions = build_suggestions(
        _state_with_hypothesis(diagnosis_name="The patient has community-acquired pneumonia")
    )

    title = suggestions[0]["title"]
    assert title == "Findings are consistent with community-acquired pneumonia"


async def test_the_reframe_is_recorded_so_the_provider_is_diagnosable():
    """The text is corrected; that it *needed* correcting is worth knowing about a provider."""
    reframed = build_suggestions(_state_with_hypothesis())[0]
    compliant = build_suggestions(
        _state_with_hypothesis(rationale="Evidence suggests a lower respiratory infection.")
    )[0]

    assert reframed["evidence"]["language_reframed"] is True
    assert compliant["evidence"]["language_reframed"] is False


async def test_reframing_does_not_escalate_the_autonomy_tier():
    """Rule #2 is about agents disagreeing on clinical risk, not about how one phrased a line.

    Escalating here would fill flag-for-review — the tier that demands active clinician
    engagement — with phrasing defects until clinicians stopped reading it.
    """
    state = _state_with_hypothesis()
    state.autonomy_tier = "suggestive"

    assert build_suggestions(state)[0]["autonomy_tier"] == "suggestive"


async def test_evidence_lines_are_left_alone():
    """ "The patient has crushing chest pain" is an accurate finding, not a certainty claim.

    Reframing it would make the record less true. The guard is scoped to the text that carries
    a conclusion or a recommendation, which is what Rule #4 is about.
    """
    state = _state_with_hypothesis()
    state.hypothesis_set[0].evidence_for.append(
        Evidence(
            text="The patient has crushing central chest pain",
            supports=True,
            source="patient_data",
        )
    )

    evidence = build_suggestions(state)[0]["evidence"]["evidence_for"]
    assert evidence[0]["text"] == "The patient has crushing central chest pain"


async def test_a_management_option_is_reframed():
    """The output type the rule is most about: it is the one that names a drug and a dose."""
    state = _case()
    state.management_options.append(
        ManagementOption(
            text="Give metformin 500 mg BD as first-line therapy.",
            sufficient_support=True,
        )
    )

    option = next(s for s in build_suggestions(state) if s["output_type"] == "management")
    assert option["body"].startswith("Guidelines support considering metformin 500 mg BD")
    assert option["evidence"]["language_reframed"] is True


async def test_a_non_string_rationale_is_still_flattened_before_it_is_framed():
    """``as_text`` first, then the framing — a dict is not a sentence and cannot be matched."""
    state = _state_with_hypothesis(
        rationale={"rationale": "Give amoxicillin 500 mg TDS.", "probability": 0.4}
    )

    assert build_suggestions(state)[0]["body"].startswith("Guidelines support considering")
