"""The Verifier gate: what the model is allowed to change, and what it is not.

``app.agents.verifier`` is the node every case must pass through before synthesis (Critical
Safety Rule #1). It computes a deterministic floor — the most conservative autonomy tier the
hard rules demand — and then lets the LLM speak. The whole safety argument for the node rests
on one asymmetry: **the model can escalate, never relax**. Nothing else in the pipeline
re-checks that, so if ``more_conservative_tier`` were ever applied the wrong way round, a case
with a drug-safety hard block could be handed back as ``informational`` and no test would fail.

Until now the node had no direct tests: the end-to-end reasoning tests exercise it only through
the offline path, where the model returns nothing and the floor is trivially the answer. These
tests give it a model that answers — including one that answers badly, which is the case the
asymmetry exists for.

The stub clients subclass the real ``LLMClient`` and override only the two methods
``app.agents.util.call_llm`` touches, so no provider key or network is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents import verifier
from app.agents.context import ReasoningContext
from app.agents.llm import LLMClient, LLMUnavailable
from app.agents.state import (
    CaseState,
    GuidelineChunkRef,
    HardBlock,
    Hypothesis,
    ManagementOption,
)


class _OfflineLLM(LLMClient):
    """Reports itself unavailable — the degraded/offline path."""

    def available(self) -> bool:
        return False

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        raise AssertionError("complete_json must not be called when available() is False")


class _CannedLLM(LLMClient):
    """Available, and returns a fixed payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        return self.payload


class _FailingLLM(LLMClient):
    """Available, then fails mid-call — the provider-outage path, distinct from offline."""

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        raise LLMUnavailable("provider timed out")


def _ctx(llm: LLMClient | None = None) -> tuple[ReasoningContext, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    client = llm or _OfflineLLM()
    return ReasoningContext(llm=client, verifier_llm=client, emit=emit), events


def _state(**kwargs: Any) -> CaseState:
    state = CaseState(patient_id="p1", presenting_complaint="Chest pain for two hours")
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


def _hypothesis(name: str, **kwargs: Any) -> Hypothesis:
    return Hypothesis(diagnosis_name=name, **kwargs)


def _citation() -> GuidelineChunkRef:
    return GuidelineChunkRef(
        section_id="STW-1",
        source="stw",
        document_title="Standard Treatment Workflows",
        heading="Dyspepsia",
        snippet="Trial of a proton-pump inhibitor.",
        score=0.9,
        corpus_version="2024.1",
    )


def _event(events: list[tuple[str, dict]], name: str) -> dict:
    matches = [data for event, data in events if event == name]
    assert len(matches) == 1, f"expected exactly one {name!r} event, got {len(matches)}"
    return matches[0]


# ------------------------------------------------------------------ the deterministic floor


def test_a_case_with_nothing_on_it_stays_informational():
    """The floor only rises for a reason. With no hypotheses and no management options there
    is no clinical claim to caveat, so the tier must not be inflated — a screen that flags
    everything is a screen that flags nothing."""
    tier, reasons = verifier._deterministic_floor(_state())

    assert tier == "informational"
    assert reasons == []


def test_having_something_to_say_lifts_the_floor_to_suggestive():
    """A differential is a clinical claim, so it cannot be presented as merely informational.

    The management-option half of the same rule is asserted with a cited option deliberately:
    an uncited one is escalated further by a later rule, so it could not distinguish "having
    something to say lifts the floor" from "uncited advice is escalated".
    """
    assert verifier._deterministic_floor(_state(hypothesis_set=[_hypothesis("Gastritis")]))[0] == (
        "suggestive"
    )
    assert verifier._deterministic_floor(
        _state(management_options=[ManagementOption(text="Start PPI", citations=[_citation()])])
    )[0] == ("suggestive")


def test_a_cant_miss_diagnosis_escalates_and_says_so():
    """The tier alone does not tell a clinician why the case was escalated. Every rule that
    raises the floor must also name itself — that string is what reaches the Reasoning Theatre
    and the session trace."""
    tier, reasons = verifier._deterministic_floor(
        _state(hypothesis_set=[_hypothesis("Acute coronary syndrome", cant_miss_flag=True)])
    )

    assert tier == "flag_for_review"
    assert "A can't-miss diagnosis is on the differential." in reasons


def test_a_drug_safety_hard_block_escalates_and_says_so():
    tier, reasons = verifier._deterministic_floor(
        _state(hard_blocks=[HardBlock(summary="Warfarin + NSAID", check_type="drug_interaction")])
    )

    assert tier == "flag_for_review"
    assert "A drug-safety hard block was triggered." in reasons


@pytest.mark.parametrize("severity", ["warning", "critical"])
def test_a_drug_safety_warning_escalates_even_without_a_hard_block(severity):
    """Hard blocks are not the only safety signal. A warning-severity flag is a reason for a
    clinician to look, so it must reach flag_for_review on its own."""
    tier, reasons = verifier._deterministic_floor(
        _state(drug_safety_flags=[{"severity": severity, "summary": "Renal dose adjustment"}])
    )

    assert tier == "flag_for_review"
    assert "A drug-safety warning is present." in reasons


@pytest.mark.parametrize("severity", ["info", "low", None])
def test_an_informational_safety_flag_does_not_escalate_on_its_own(severity):
    """The other side of the same rule. If every flag escalated, the tier would carry no
    information and clinicians would stop reading it."""
    tier, reasons = verifier._deterministic_floor(
        _state(drug_safety_flags=[{"severity": severity, "summary": "Take with food"}])
    )

    assert tier == "informational"
    assert reasons == []


def test_management_advice_with_no_citation_behind_it_is_escalated():
    """Uncited management advice is the failure mode this product exists to prevent: it looks
    identical to cited advice in the UI. It must not be presented at the tier that says a
    clinician can act on it lightly."""
    tier, reasons = verifier._deterministic_floor(
        _state(management_options=[ManagementOption(text="Start metformin", citations=[])])
    )

    assert tier == "flag_for_review"
    assert "Management options lack adequate guideline support." in reasons


def test_degraded_reasoning_is_itself_a_reason_to_escalate():
    """Output built without the LLM is still output. Saying so, and escalating, is what keeps
    a degraded run from reading like a full one."""
    tier, reasons = verifier._deterministic_floor(_state(degraded=True))

    assert tier == "flag_for_review"
    assert any("degraded mode" in r for r in reasons)


# ------------------------------------------------------------------ the escalate-only rule


async def test_the_model_cannot_lower_a_tier_the_hard_rules_raised():
    """The core safety asymmetry, stated directly.

    A case with a drug-safety hard block floors at flag_for_review. A model that answers
    "informational" — whether from a bad prompt, a bad sample, or an injected instruction in
    the patient record — must change nothing.
    """
    state = _state(
        hard_blocks=[HardBlock(summary="Warfarin + diclofenac", check_type="drug_interaction")],
        hypothesis_set=[_hypothesis("Peptic ulcer")],
    )
    ctx, events = _ctx(_CannedLLM({"status": "agree", "autonomy_tier": "informational"}))

    await verifier.run(state, ctx)

    assert state.autonomy_tier == "flag_for_review"
    assert _event(events, "verifier")["autonomy_tier"] == "flag_for_review"


async def test_the_model_can_raise_a_tier_the_hard_rules_left_low():
    """The permitted direction. Nothing in the record demands escalation, but the model saw
    something the rule table does not encode — and escalation is always allowed."""
    state = _state(hypothesis_set=[_hypothesis("Tension headache")])
    ctx, _ = _ctx(_CannedLLM({"status": "agree", "autonomy_tier": "flag_for_review"}))

    await verifier.run(state, ctx)

    assert state.autonomy_tier == "flag_for_review"


@pytest.mark.parametrize("bad_tier", ["", "auto", "APPROVED", "informational ", None, 3, {"t": 1}])
async def test_a_tier_the_model_invented_is_discarded_rather_than_trusted(bad_tier):
    """An unrecognised tier is not a lower tier — it is no answer at all. Rejecting it leaves
    the deterministic floor standing, which is the safe default; coercing it would let any
    malformed string decide how a case is presented.
    """
    state = _state(hypothesis_set=[_hypothesis("Migraine", cant_miss_flag=True)])
    ctx, _ = _ctx(_CannedLLM({"status": "agree", "autonomy_tier": bad_tier}))

    await verifier.run(state, ctx)

    assert state.autonomy_tier == "flag_for_review"


@pytest.mark.parametrize("bad_status", ["", "approved", "DISAGREE", None, 7, ["agree"]])
async def test_a_status_the_model_invented_falls_back_to_agree(bad_status):
    """``verifier_status`` drives a routing decision in the graph — disagreement runs the
    conservative-resolution node. An unrecognised value must land on a defined status rather
    than reaching that comparison as arbitrary text.
    """
    state = _state(hypothesis_set=[_hypothesis("Migraine")])
    ctx, _ = _ctx(_CannedLLM({"status": bad_status, "autonomy_tier": "suggestive"}))

    await verifier.run(state, ctx)

    assert state.verifier_status == "agree"


@pytest.mark.parametrize("status", ["partial_disagreement", "major_disagreement"])
async def test_a_recognised_disagreement_is_carried_through_untouched(status):
    """The value the graph routes on. Both disagreement statuses must survive the node exactly
    as given, or the conservative-resolution edge never fires."""
    state = _state(hypothesis_set=[_hypothesis("Migraine")])
    ctx, _ = _ctx(_CannedLLM({"status": status, "autonomy_tier": "suggestive"}))

    await verifier.run(state, ctx)

    assert state.verifier_status == status


# ------------------------------------------------------------------ specialist disagreement


def _three_way_split() -> list[Hypothesis]:
    """Three leading hypotheses from two different specialists — the disagreement shape."""
    return [
        _hypothesis("Acute coronary syndrome", probability_band="moderate", source_agent="cardio"),
        _hypothesis("Pulmonary embolism", probability_band="moderate", source_agent="pulm"),
        _hypothesis("Costochondritis", probability_band="low", source_agent="cardio"),
    ]


async def test_specialists_disagreeing_escalates_even_when_the_verifier_agreed():
    """Two independent escalation paths, and the union wins.

    The verifier LLM can be perfectly happy with a case the specialist agents could not agree
    on. Unresolved disagreement between the reasoning agents is exactly the situation a
    clinician should be looking at, so it escalates on its own evidence.
    """
    state = _state(hypothesis_set=_three_way_split())
    ctx, events = _ctx(_CannedLLM({"status": "agree", "autonomy_tier": "suggestive"}))

    await verifier.run(state, ctx)

    assert state.verifier_status == "partial_disagreement"
    assert state.autonomy_tier == "flag_for_review"
    assert (
        "Specialist agents disagreed on the leading hypothesis."
        in (_event(events, "verifier")["case_caveats"])
    )


async def test_specialist_disagreement_does_not_soften_a_major_disagreement():
    """``partial_disagreement`` is the *weaker* of the two statuses. Overwriting a
    ``major_disagreement`` with it would relax the verdict — which is the one thing this node
    is not allowed to do."""
    state = _state(hypothesis_set=_three_way_split())
    ctx, _ = _ctx(_CannedLLM({"status": "major_disagreement", "autonomy_tier": "suggestive"}))

    await verifier.run(state, ctx)

    assert state.verifier_status == "major_disagreement"


def test_one_specialist_naming_three_diagnoses_is_not_a_disagreement():
    """A single agent listing a broad differential is ordinary reasoning, not a conflict. If
    that counted, every well-populated differential would escalate and the signal would be
    worthless."""
    state = _state(
        hypothesis_set=[
            _hypothesis("A", probability_band="moderate", source_agent="cardio"),
            _hypothesis("B", probability_band="moderate", source_agent="cardio"),
            _hypothesis("C", probability_band="low", source_agent="cardio"),
        ]
    )

    assert verifier._specialists_disagree(state) is False


def test_the_sentinels_own_hypotheses_do_not_count_as_a_dissenting_specialist():
    """The can't-miss sentinel appends to *every* differential by design, so counting it as a
    source would make two agents out of one and mark almost every case as disputed."""
    state = _state(
        hypothesis_set=[
            _hypothesis("A", probability_band="moderate", source_agent="cardio"),
            _hypothesis("B", probability_band="moderate", source_agent="sentinel"),
            _hypothesis("C", probability_band="low", source_agent="sentinel"),
        ]
    )

    assert verifier._specialists_disagree(state) is False


def test_two_specialists_naming_the_same_two_diagnoses_is_not_a_disagreement():
    """Agreement on a two-item differential, reached independently, is corroboration. The rule
    needs three distinct leading names before it reads the split as unresolved."""
    state = _state(
        hypothesis_set=[
            _hypothesis("A", probability_band="moderate", source_agent="cardio"),
            _hypothesis("A", probability_band="moderate", source_agent="pulm"),
        ]
    )

    assert verifier._specialists_disagree(state) is False


# ------------------------------------------------------------------ verdicts and degradation


async def test_a_case_the_model_did_not_answer_still_gets_a_verdict_with_its_reasons():
    """Offline, the node must still produce a verdict — an empty ``verifier_verdicts`` would
    leave every synthesised suggestion with no verdict to show. The floor's reasons ride on it,
    so the record says why the case was escalated even with no model in the loop."""
    state = _state(hypothesis_set=[_hypothesis("Sepsis", cant_miss_flag=True)])
    ctx, _ = _ctx(_OfflineLLM())

    await verifier.run(state, ctx)

    assert len(state.verifier_verdicts) == 1
    verdict = state.verifier_verdicts[0]
    assert verdict.target == "case"
    assert "A can't-miss diagnosis is on the differential." in verdict.caveats


async def test_an_unavailable_model_marks_the_run_degraded():
    """``degraded`` is itself an escalation reason elsewhere in the pipeline, and it is the
    flag the UI uses to tell a clinician the output was built without AI reasoning."""
    state = _state(hypothesis_set=[_hypothesis("Sepsis")])
    ctx, _ = _ctx(_OfflineLLM())

    await verifier.run(state, ctx)

    assert state.degraded is True


async def test_a_provider_outage_mid_call_is_not_recorded_as_degraded_reasoning():
    """A client that reports itself available and then fails is a different fact from one that
    was never configured. ``degraded`` tracks "no AI in the loop"; ``ctx.llm_available()`` is
    still true here, so the node falls back for this call without relabelling the whole run.
    What matters for safety is that the floor still stands, which is asserted alongside.
    """
    state = _state(hypothesis_set=[_hypothesis("Sepsis", cant_miss_flag=True)])
    ctx, _ = _ctx(_FailingLLM())

    await verifier.run(state, ctx)

    assert state.degraded is False
    assert state.autonomy_tier == "flag_for_review"
    assert state.verifier_verdicts[0].target == "case"


async def test_a_run_already_marked_degraded_is_never_un_marked():
    """An earlier agent losing the model is not undone by the verifier reaching one."""
    state = _state(degraded=True, hypothesis_set=[_hypothesis("Sepsis")])
    ctx, _ = _ctx(_CannedLLM({"status": "agree", "autonomy_tier": "suggestive"}))

    await verifier.run(state, ctx)

    assert state.degraded is True


async def test_model_supplied_verdicts_are_kept_per_target_and_sanitised():
    """Per-hypothesis verdicts are what let the UI show a check against a specific diagnosis.
    They are taken as given except for the status, which is a routing value and so is held to
    the same allow-list as the case status.
    """
    state = _state(hypothesis_set=[_hypothesis("Angina"), _hypothesis("Reflux")])
    ctx, _ = _ctx(
        _CannedLLM(
            {
                "status": "partial_disagreement",
                "autonomy_tier": "suggestive",
                "verdicts": [
                    {
                        "target": "Angina",
                        "status": "major_disagreement",
                        "rationale": "Troponin not available.",
                        "caveats": ["No ECG on file."],
                    },
                    {"target": "Reflux", "status": "not_a_status", "rationale": "Plausible."},
                ],
            }
        )
    )

    await verifier.run(state, ctx)

    by_target = {v.target: v for v in state.verifier_verdicts}
    assert by_target["Angina"].status == "major_disagreement"
    assert by_target["Angina"].caveats == ["No ECG on file."]
    assert by_target["Reflux"].status == "agree"
    assert by_target["Reflux"].caveats == []


@pytest.mark.parametrize(
    "payload",
    [
        {"status": ["agree"], "autonomy_tier": "suggestive"},
        {"status": "agree", "autonomy_tier": {"tier": "informational"}},
        {"status": {"value": "agree"}, "autonomy_tier": ["flag_for_review"]},
        {"status": "agree", "autonomy_tier": "suggestive", "verdicts": {"target": "Angina"}},
        {"status": "agree", "autonomy_tier": "suggestive", "verdicts": ["Angina looks right"]},
        {"status": "agree", "autonomy_tier": "suggestive", "case_caveats": 42},
        {
            "status": "agree",
            "autonomy_tier": "suggestive",
            "verdicts": [{"target": "Angina", "status": {"s": "agree"}, "caveats": 7}],
        },
    ],
)
async def test_a_container_where_a_string_was_asked_for_does_not_take_the_gate_down(payload):
    """The failure this guards is an exception, not a wrong answer.

    Membership tests here run against sets, so an unhashable value from a non-conforming model
    — ``{"status": ["agree"]}`` — raised TypeError out of ``run``. The verifier is the one node
    the graph has no edge around, so that killed the whole case rather than falling back to the
    deterministic floor. ``app.agents.util.as_text`` exists because models really do return an
    object where a string was asked for; this is the same class of input reaching the gate.

    Every variant must leave the floor standing, which for a can't-miss differential is
    flag_for_review.
    """
    state = _state(hypothesis_set=[_hypothesis("Acute coronary syndrome", cant_miss_flag=True)])
    ctx, _ = _ctx(_CannedLLM(payload))

    await verifier.run(state, ctx)

    assert state.autonomy_tier == "flag_for_review"
    assert state.verifier_status in {"agree", "partial_disagreement", "major_disagreement"}
    assert state.verifier_verdicts, "the gate must always leave a verdict behind"


async def test_a_caveat_string_is_one_caveat_and_not_a_list_of_letters():
    """``list("Limited history")`` is fifteen caveats, each one character long — which is what
    a bare string used to become on its way to the Reasoning Theatre."""
    state = _state(hypothesis_set=[_hypothesis("Angina")])
    ctx, events = _ctx(_CannedLLM({"status": "agree", "case_caveats": "History is second-hand."}))

    await verifier.run(state, ctx)

    assert _event(events, "verifier")["case_caveats"] == ["History is second-hand."]


async def test_a_null_entry_among_the_caveats_is_dropped_rather_than_rendered():
    """Nulls in this list are observed, not hypothetical — the Theatre component carries its
    own guard for them. Dropping them here means the UI is not the only thing standing between
    a null and a blank bullet point in a clinician's caveat list."""
    state = _state(hypothesis_set=[_hypothesis("Angina")])
    ctx, events = _ctx(
        _CannedLLM({"status": "agree", "case_caveats": [None, "Limited history", "", "  "]})
    )

    await verifier.run(state, ctx)

    assert _event(events, "verifier")["case_caveats"] == ["Limited history"]


async def test_a_verdict_the_model_sent_with_no_fields_at_all_does_not_crash_the_gate():
    """Malformed output must degrade to a usable verdict, not an exception: an unhandled error
    here takes down the one node the pipeline cannot route around."""
    state = _state(hypothesis_set=[_hypothesis("Angina")])
    ctx, _ = _ctx(_CannedLLM({"verdicts": [{}]}))

    await verifier.run(state, ctx)

    assert [(v.target, v.status) for v in state.verifier_verdicts] == [("", "agree")]
    assert state.verifier_status == "agree"


async def test_the_model_may_add_case_caveats_but_not_replace_the_floors():
    """Both sets of caveats reach the clinician. The floor's reasons are the auditable half —
    they are the ones a rule fired on — so a chatty model must not be able to crowd them out.
    """
    state = _state(hypothesis_set=[_hypothesis("Sepsis", cant_miss_flag=True)])
    ctx, events = _ctx(
        _CannedLLM(
            {
                "status": "agree",
                "autonomy_tier": "suggestive",
                "case_caveats": ["History is second-hand."],
            }
        )
    )

    await verifier.run(state, ctx)

    caveats = _event(events, "verifier")["case_caveats"]
    assert "A can't-miss diagnosis is on the differential." in caveats
    assert "History is second-hand." in caveats


# ------------------------------------------------------------------ the record it leaves


async def test_the_node_always_opens_and_closes_its_lane():
    """The Reasoning Theatre draws a lane per agent from these two events. A path that returns
    without emitting ``agent_complete`` leaves the verifier lane spinning forever — on the one
    node whose completion is the clinician's signal that the output was checked."""
    state = _state(hypothesis_set=[_hypothesis("Angina")])
    ctx, events = _ctx(_CannedLLM({"status": "agree", "autonomy_tier": "suggestive"}))

    await verifier.run(state, ctx)

    names = [event for event, _ in events]
    assert names[0] == "agent_start"
    assert names[-1] == "agent_complete"
    assert _event(events, "agent_start")["agent"] == verifier.AGENT


async def test_the_trace_records_the_verdict_and_the_reasons_behind_it():
    """``agent_trace`` is persisted into the immutable session snapshot, so it is where an
    audit reads back why a case was escalated after the live stream is gone."""
    state = _state(hypothesis_set=[_hypothesis("Sepsis", cant_miss_flag=True)])
    ctx, _ = _ctx(_OfflineLLM())

    await verifier.run(state, ctx)

    entry = next(t for t in state.agent_trace if t.agent == verifier.AGENT)
    assert "flag_for_review" in entry.summary
    assert "A can't-miss diagnosis is on the differential." in entry.detail["caveats"]


async def test_the_emitted_verdict_list_matches_the_state_it_leaves_behind():
    """The live stream and the persisted state are two renderings of one decision. They must
    not be able to disagree — a clinician watching the Theatre and one reopening the session
    later have to see the same verdicts."""
    state = _state(hypothesis_set=[_hypothesis("Angina"), _hypothesis("Reflux")])
    ctx, events = _ctx(
        _CannedLLM(
            {
                "status": "agree",
                "autonomy_tier": "suggestive",
                "verdicts": [
                    {"target": "Angina", "status": "agree", "rationale": "Consistent."},
                    {"target": "Reflux", "status": "agree", "rationale": "Also consistent."},
                ],
            }
        )
    )

    await verifier.run(state, ctx)

    emitted = _event(events, "verifier")["verdicts"]
    assert [v["target"] for v in emitted] == [v.target for v in state.verifier_verdicts]
    assert [v["status"] for v in emitted] == [v.status for v in state.verifier_verdicts]
