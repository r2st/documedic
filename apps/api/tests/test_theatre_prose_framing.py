"""Critical Safety Rule #4 covers the model prose the Theatre shows, not only the suggestion.

``tests/test_clinical_language_framing`` pins the rewrite itself and its wiring into
``agents.synthesis``, which frames the two strings a ``ClinicalSuggestion`` is built out of —
the title and the body. That was the whole of the enforcement, and it left three model-written
clinical fields untouched:

* the Devil's-Advocate critique (``summary``, ``base_rate_caveat``, ``alternative_explanations``)
* the Investigation Strategist's per-test ``rationale``
* the Verifier's verdict ``rationale`` and its ``caveats``

All three are assembled at their own node, emitted as their own SSE event, and written into the
``case_state`` snapshot — so the Reasoning Theatre renders them live, minutes before synthesis
runs. Framing them downstream would leave the sentence the clinician actually read untouched and
then copy it verbatim into a row that is immutable by trigger. The Devil's-Advocate is the worst
of the three: Rule #5 puts it on screen for every case, uncollapsed, by construction, which makes
it the model prose most certain to be read.

What is asserted here is the two-sided contract the framing module is built around: the modality
moves, and the clinical content does not. A rewrite that dropped a drug or a diagnosis would be a
worse defect than the imperative it was fixing.

Deliberately *not* framed, and asserted as such below: ``disconfirming_evidence`` (evidence lines
are findings, not claims — see ``app.core.clinical_language``) and ``VerifierVerdict.target``
(a name that has to keep matching a hypothesis for ``synthesis._verdict_for`` to attach the
verdict to the right card).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents import devils_advocate, investigation_strategist, verifier
from app.agents.context import ReasoningContext
from app.agents.llm import LLMClient
from app.agents.state import CaseState, Hypothesis
from app.agents.synthesis import build_suggestions
from app.core.clinical_language import has_certainty_language, prescriber_framed_list

pytestmark = pytest.mark.asyncio


class _CannedLLM(LLMClient):
    """Returns one fixed payload, standing in for a provider that ignored its framing rules."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        return self.payload


def _ctx(payload: dict[str, Any]) -> tuple[ReasoningContext, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    client = _CannedLLM(payload)
    return ReasoningContext(llm=client, verifier_llm=client, emit=emit), events


def _event(events: list[tuple[str, dict]], name: str) -> dict:
    return next(data for event, data in events if event == name)


def _critique_of(state: CaseState) -> dict[str, Any]:
    """The critique attached to the leading hypothesis. Absent is a failure, not a skip."""
    critique = state.hypothesis_set[0].devil_advocate
    assert critique is not None, "the Devil's-Advocate attached nothing to the leader"
    return critique


def _case_with_leader() -> CaseState:
    state = CaseState(patient_id="p1", presenting_complaint="Central chest pain since this morning")
    state.hypothesis_set.append(
        Hypothesis(
            diagnosis_name="Acute coronary syndrome",
            probability_band="moderate",
            source_agent="cardiology",
        )
    )
    return state


# --------------------------------------------------------- the list helper


async def test_each_item_in_a_list_is_framed_on_its_own():
    """Joining and re-splitting would break the sentence-start anchor every rule relies on."""
    framed, changed = prescriber_framed_list(
        ["Give aspirin 300 mg.", "Evidence suggests a lower respiratory infection."]
    )

    assert changed is True
    assert framed[0] == "Guidelines support considering aspirin 300 mg."
    assert framed[1] == "Evidence suggests a lower respiratory infection."


async def test_a_compliant_list_reports_no_change():
    framed, changed = prescriber_framed_list(["Consider evaluating for pulmonary embolism."])

    assert changed is False
    assert framed == ["Consider evaluating for pulmonary embolism."]


async def test_an_empty_list_is_not_a_change():
    assert prescriber_framed_list([]) == ([], False)


# --------------------------------------------------------- Devil's-Advocate (Rule #5)


async def test_the_dissent_the_clinician_always_sees_is_framed():
    """Rule #5 guarantees this is on screen; that makes its modality the one that matters most."""
    state = _case_with_leader()
    ctx, events = _ctx(
        {
            "summary": "The patient has gastro-oesophageal reflux, not ACS.",
            "base_rate_caveat": "Diagnosis is reflux in this age group.",
            "alternative_explanations": [
                "Give a PPI trial to confirm reflux.",
                "Musculoskeletal chest wall pain.",
            ],
            "disconfirming_evidence": ["The patient has no exertional component."],
        }
    )

    await devils_advocate.run(state, ctx)
    critique = _event(events, "devils_advocate")["critique"]

    assert critique["summary"] == (
        "Findings are consistent with gastro-oesophageal reflux, not ACS."
    )
    assert critique["base_rate_caveat"] == "Findings are consistent with reflux in this age group."
    assert critique["alternative_explanations"][0] == (
        "Guidelines support considering a PPI trial to confirm reflux."
    )
    assert critique["language_reframed"] is True


async def test_the_dissent_keeps_its_clinical_content_through_the_rewrite():
    """Modality only. Losing the alternative diagnosis would be worse than the imperative."""
    state = _case_with_leader()
    ctx, _ = _ctx(
        {
            "summary": "The patient has aortic dissection.",
            "alternative_explanations": ["Diagnose with pericarditis."],
        }
    )

    await devils_advocate.run(state, ctx)
    critique = _critique_of(state)

    assert "aortic dissection" in critique["summary"]
    assert "pericarditis" in critique["alternative_explanations"][0]
    assert not has_certainty_language(critique["summary"])


async def test_disconfirming_evidence_is_left_exactly_as_it_stands():
    """An evidence line is a finding. Hedging it makes the record less true, not more careful."""
    state = _case_with_leader()
    ctx, _ = _ctx(
        {
            "summary": "Consider alternatives.",
            "disconfirming_evidence": ["The patient has a normal troponin at six hours."],
        }
    )

    await devils_advocate.run(state, ctx)

    assert _critique_of(state)["disconfirming_evidence"] == [
        "The patient has a normal troponin at six hours."
    ]


async def test_compliant_dissent_is_not_marked_as_reframed():
    state = _case_with_leader()
    ctx, _ = _ctx(
        {
            "summary": "Findings remain compatible with a non-cardiac cause.",
            "base_rate_caveat": "Consider local base rates before reassurance.",
            "alternative_explanations": ["Musculoskeletal chest wall pain"],
        }
    )

    await devils_advocate.run(state, ctx)

    assert _critique_of(state)["language_reframed"] is False


async def test_the_offline_critique_reports_the_same_key():
    """Whichever branch produced it, the UI receives a critique with the same shape."""
    state = _case_with_leader()

    critique = devils_advocate._fallback(state, "Acute coronary syndrome")

    assert critique["language_reframed"] is False
    assert not has_certainty_language(critique["summary"])


async def test_the_framed_dissent_reaches_the_immutable_suggestion():
    """The Theatre and the persisted row must not disagree about what the model said."""
    state = _case_with_leader()
    ctx, _ = _ctx({"summary": "The patient has reflux.", "alternative_explanations": []})

    await devils_advocate.run(state, ctx)
    suggestion = build_suggestions(state)[0]

    assert suggestion["devils_advocate"]["summary"] == "Findings are consistent with reflux."


# --------------------------------------------------------- Investigation Strategist


async def test_an_investigation_rationale_is_framed():
    """ "Start empirical antibiotics while awaiting culture" is a natural way to justify a test.

    It is also a prescribing instruction, and it lands on the investigation card verbatim.
    """
    state = _case_with_leader()
    ctx, events = _ctx(
        {
            "investigations": [
                {
                    "name": "High-sensitivity troponin",
                    "rationale": "Start empirical anticoagulation while awaiting the result.",
                    "expected_information_gain": "high",
                }
            ]
        }
    )

    await investigation_strategist.run(state, ctx)
    investigation = _event(events, "investigations")["investigations"][0]

    assert investigation["rationale"] == (
        "Guidelines support considering empirical anticoagulation while awaiting the result."
    )
    # The test's own name is a noun, not a claim, and is left alone.
    assert investigation["name"] == "High-sensitivity troponin"


async def test_the_framed_rationale_reaches_the_suggestion_card():
    state = _case_with_leader()
    ctx, _ = _ctx(
        {"investigations": [{"name": "Chest X-ray", "rationale": "The patient has pneumonia."}]}
    )

    await investigation_strategist.run(state, ctx)
    card = next(s for s in build_suggestions(state) if s["output_type"] == "investigation")

    assert card["evidence"]["investigations"][0]["rationale"] == (
        "Findings are consistent with pneumonia."
    )


# --------------------------------------------------------- Verifier (the gate)


async def test_the_gates_own_prose_is_framed_like_everyone_elses():
    """A certainty claim is most plausible in a caveat — it reads as diligence.

    And the Verifier's word carries more weight with the clinician than any other agent's, which
    is why its modality is checked rather than trusted because of who said it.
    """
    state = _case_with_leader()
    ctx, events = _ctx(
        {
            "status": "agree",
            "autonomy_tier": "suggestive",
            "verdicts": [
                {
                    "target": "Acute coronary syndrome",
                    "status": "agree",
                    "rationale": "The patient has an evolving infarct.",
                    "caveats": ["Give aspirin before transfer."],
                }
            ],
            "case_caveats": ["Diagnosis is time-critical."],
        }
    )

    await verifier.run(state, ctx)
    emitted = _event(events, "verifier")

    assert emitted["verdicts"][0]["rationale"] == (
        "Findings are consistent with an evolving infarct."
    )
    assert state.verifier_verdicts[0].caveats == [
        "Guidelines support considering aspirin before transfer."
    ]
    assert "Findings are consistent with time-critical." in emitted["case_caveats"]


async def test_the_verdict_target_is_not_rewritten():
    """It is the key ``synthesis._verdict_for`` matches on; rewriting it detaches the verdict."""
    state = _case_with_leader()
    ctx, _ = _ctx(
        {
            "status": "agree",
            "verdicts": [
                {
                    "target": "Acute coronary syndrome",
                    "status": "agree",
                    "rationale": "Supported by the presentation.",
                }
            ],
        }
    )

    await verifier.run(state, ctx)
    suggestion = build_suggestions(state)[0]

    assert state.verifier_verdicts[0].target == "Acute coronary syndrome"
    assert suggestion["verifier_verdict"]["rationale"] == "Supported by the presentation."


async def test_the_deterministic_floor_reasons_survive_framing_unchanged():
    """Our own caveats are already prescriber-framed; the pass over them must be a no-op.

    ``prescriber_framed`` is idempotent by design, and the floor is what stands when the gate's
    provider is down — a guard that mangled it would damage the one output that is always right.
    """
    state = _case_with_leader()
    state.hypothesis_set[0].cant_miss_flag = True
    ctx, events = _ctx({"status": "agree", "verdicts": [], "case_caveats": []})

    await verifier.run(state, ctx)

    assert (
        "A can't-miss diagnosis is on the differential."
        in _event(events, "verifier")["case_caveats"]
    )
