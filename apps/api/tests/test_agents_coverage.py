"""Agent-node branches the pipeline tests never reach.

Every test here targets a line the full-suite coverage report listed as missing in
``app/agents/``. They are mostly the "the model gave us something odd" and "there is nothing
to work with" branches — the ones that decide whether a safety signal survives or is dropped.

The stub LLM clients below subclass the real ``LLMClient`` and override only the two methods
``app.agents.util.call_llm`` uses, so no provider key (or network) is involved.
"""

from __future__ import annotations

from typing import Any

from app.agents import cant_miss_sentinel, devils_advocate, guideline_rag, investigation_strategist
from app.agents import hypothesis_panel as panel
from app.agents.context import ReasoningContext, _empty_retriever, _empty_safety
from app.agents.llm import LLMClient
from app.agents.state import CaseState, Evidence, Hypothesis, VerifierVerdict
from app.agents.synthesis import _verdict_for
from app.config import settings


class _OfflineLLM(LLMClient):
    """A client that reports itself unavailable, forcing the deterministic path."""

    def available(self) -> bool:
        return False

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        raise AssertionError("complete_json must not be called when available() is False")


class _CannedLLM(LLMClient):
    """A client that returns a fixed payload, standing in for a real model response."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        return self.payload


def _ctx(
    llm: LLMClient | None = None, **kwargs: Any
) -> tuple[ReasoningContext, list[tuple[str, dict]]]:
    """A context whose emitted events are captured for assertion."""
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    client = llm or _OfflineLLM()
    return ReasoningContext(llm=client, verifier_llm=client, emit=emit, **kwargs), events


# --------------------------------------------------------------- can't-miss sentinel


async def test_sentinel_flags_a_matching_existing_hypothesis_instead_of_duplicating_it():
    """A can't-miss rule that names an already-listed diagnosis must flag it in place.

    Without this the differential would carry the same diagnosis twice — and, worse, the copy
    the UI ranks and shows might be the unflagged one, losing the can't-miss marker.
    """
    state = CaseState(
        patient_id="p1",
        presenting_complaint="Chest pain radiating to the left arm since this morning",
        hypothesis_set=[
            Hypothesis(diagnosis_name="Acute coronary syndrome", probability_band="moderate")
        ],
    )
    ctx, _ = _ctx()

    await cant_miss_sentinel.run(state, ctx)

    acs = [h for h in state.hypothesis_set if h.diagnosis_name == "Acute coronary syndrome"]
    assert len(acs) == 1, "the sentinel duplicated a diagnosis already on the differential"
    assert acs[0].cant_miss_flag is True
    assert acs[0].rationale is not None and acs[0].rationale.startswith("Can't-miss:")
    # The pre-existing band is preserved — the sentinel only adds the flag.
    assert acs[0].probability_band == "moderate"


async def test_sentinel_preserves_a_rationale_the_panel_already_wrote():
    """Flagging in place must not overwrite the specialist's reasoning with the rule text."""
    state = CaseState(
        patient_id="p1",
        presenting_complaint="Chest pain radiating to the left arm",
        hypothesis_set=[
            Hypothesis(
                diagnosis_name="acute coronary syndrome",  # case-insensitive match
                rationale="Typical exertional pattern with diabetic risk factors.",
            )
        ],
    )
    ctx, _ = _ctx()

    await cant_miss_sentinel.run(state, ctx)

    kept = state.hypothesis_set[0]
    assert kept.cant_miss_flag is True
    assert kept.rationale == "Typical exertional pattern with diabetic risk factors."


# --------------------------------------------------------------- reasoning context defaults


def test_default_retriever_and_safety_evaluator_return_empty_rather_than_raising():
    """The zero-arg context must be usable: agents call these before anything is injected."""
    assert _empty_retriever("management of dengue", 6) == []
    assert _empty_safety("Paracetamol") == []
    ctx = ReasoningContext(llm=_OfflineLLM(), verifier_llm=_OfflineLLM())
    assert ctx.retrieve("anything", 3) == []
    assert ctx.evaluate_safety("anything") == []


# --------------------------------------------------------------- devil's advocate


async def test_devils_advocate_completes_cleanly_when_there_is_no_leading_hypothesis():
    """With an empty differential there is nothing to attack, but the agent must still close.

    Returning without an ``agent_complete`` event would leave the Reasoning Theatre showing a
    permanently in-flight agent, which reads as "still thinking" rather than "nothing to say".
    """
    state = CaseState(patient_id="p1", presenting_complaint="Vague malaise")
    ctx, events = _ctx()

    await devils_advocate.run(state, ctx)

    assert [e for e, _ in events] == ["agent_start", "agent_complete"]
    assert state.agent_trace == []
    assert state.hypothesis_set == []


# --------------------------------------------------------------- guideline RAG


def _chunk(section_id: str = "STW-1") -> dict[str, Any]:
    return {
        "section_id": section_id,
        "source": "icmr",
        "document_title": "ICMR STW: Dengue",
        "heading": "Management",
        "content": "Maintain hydration; monitor platelet count and haematocrit.",
        "score": 0.95,
        "corpus_version": "icmr-2024.1",
        "page_range": "12-13",
    }


async def test_guideline_rag_drops_an_option_whose_text_is_blank():
    """A model returning an empty option text must not create a blank management card."""
    state = CaseState(patient_id="p1", presenting_complaint="Fever with warning signs")
    llm = _CannedLLM(
        {
            "insufficient_support": False,
            "options": [
                {"text": "   ", "citation_section_ids": ["STW-1"]},
                {
                    "text": "Guidelines support considering oral rehydration.",
                    "citation_section_ids": ["STW-1"],
                },
            ],
        }
    )
    ctx, _ = _ctx(llm, retrieve=lambda _query, _k: [_chunk()])

    await guideline_rag.run(state, ctx)

    texts = [o.text for o in state.management_options]
    assert texts == ["Guidelines support considering oral rehydration."]


async def test_guideline_rag_demo_net_supplies_clearly_marked_options_when_no_corpus_exists(
    monkeypatch,
):
    """With no provider keys and no retrieval, the demo net must fill in *marked* options.

    The marking matters more than the content: an unmarked illustrative option is
    indistinguishable from a real cited guideline, which is exactly the confusion the
    persistent demo banner exists to prevent.
    """
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_demo_fallback", True)

    state = CaseState(
        patient_id="p1",
        presenting_complaint="Fever and cough for three days",
        hypothesis_set=[Hypothesis(diagnosis_name="Community-acquired pneumonia")],
    )
    ctx, _ = _ctx()  # offline client -> no LLM branch, retriever returns nothing

    await guideline_rag.run(state, ctx)

    assert state.demo_mode is True
    assert state.management_options, "the demo net produced no options"
    for opt in state.management_options:
        assert opt.citations, "a demo option carried no citation ref"
        assert opt.citations[0].source == "demo"
        assert "DEMO" in opt.citations[0].document_title.upper()


# --------------------------------------------------------------- hypothesis panel


async def test_specialist_returns_nothing_when_the_model_is_unavailable():
    state = CaseState(patient_id="p1", presenting_complaint="Fever")
    ctx, _ = _ctx()
    assert await panel._run_specialist(state, ctx, "internal_medicine", "IM", "summary") == []


async def test_specialist_skips_a_hypothesis_with_no_diagnosis_name():
    """A nameless hypothesis cannot be rendered or verified, so it must be dropped."""
    state = CaseState(patient_id="p1", presenting_complaint="Fever")
    llm = _CannedLLM(
        {
            "hypotheses": [
                {"diagnosis_name": "", "probability_band": "high"},
                {"diagnosis_name": None},
                {"diagnosis_name": "Dengue fever", "probability_band": "moderate"},
            ]
        }
    )
    ctx, _ = _ctx(llm)

    out = await panel._run_specialist(state, ctx, "internal_medicine", "IM", "summary")

    assert [h.diagnosis_name for h in out] == ["Dengue fever"]
    assert out[0].source_agent == "internal_medicine"


def test_parse_evidence_flattens_a_bare_string_item():
    """Some models return evidence as plain strings instead of ``{"text": ...}`` objects."""
    out = panel._parse_evidence(["Platelets 78,000", {"text": "Haematocrit rising"}], True)

    assert [e.text for e in out] == ["Platelets 78,000", "Haematocrit rising"]
    assert all(e.supports is True for e in out)
    assert out[0].source_ref is None


def test_merge_promotes_the_more_confident_band_and_unions_both_evidence_lists():
    """Duplicate diagnoses from different specialists must combine, not compete.

    Dropping one specialist's evidence would silently discard a perspective the Reasoning
    Theatre is required to show.
    """
    merged = panel._merge(
        [
            Hypothesis(
                diagnosis_name="Dengue fever",
                probability_band="low",
                evidence_for=[Evidence("Fever 4 days", True)],
                evidence_against=[Evidence("No rash", False)],
            ),
            Hypothesis(
                diagnosis_name="dengue fever",  # same diagnosis, different casing
                probability_band="high",
                evidence_for=[Evidence("Platelets 78,000", True)],
                evidence_against=[Evidence("No travel history", False)],
            ),
        ]
    )

    assert len(merged) == 1
    assert merged[0].probability_band == "high"
    assert {e.text for e in merged[0].evidence_for} == {"Fever 4 days", "Platelets 78,000"}
    assert {e.text for e in merged[0].evidence_against} == {"No rash", "No travel history"}


def test_merge_never_downgrades_an_already_confident_band():
    merged = panel._merge(
        [
            Hypothesis(diagnosis_name="Dengue fever", probability_band="high"),
            Hypothesis(diagnosis_name="Dengue fever", probability_band="insufficient_data"),
        ]
    )

    assert len(merged) == 1
    assert merged[0].probability_band == "high"


# --------------------------------------------------------------- investigation strategist


async def test_strategist_skips_an_investigation_with_no_name():
    state = CaseState(patient_id="p1", presenting_complaint="Fever")
    llm = _CannedLLM(
        {
            "investigations": [
                {"name": "  ", "rationale": "nameless"},
                {
                    "name": "NS1 antigen",
                    "rationale": "Discriminates dengue early",
                    "availability_tier": "phc",
                },
            ]
        }
    )
    ctx, _ = _ctx(llm)

    await investigation_strategist.run(state, ctx)

    assert [i.name for i in state.recommended_investigations] == ["NS1 antigen"]


# --------------------------------------------------------------- state / synthesis


def test_add_message_records_the_agent_message_with_a_default_data_dict():
    state = CaseState(patient_id="p1", presenting_complaint="Fever")

    state.add_message("verifier", "verdict", "Agrees with the differential")

    assert len(state.agent_messages) == 1
    msg = state.agent_messages[0]
    assert (msg.agent, msg.role, msg.content) == (
        "verifier",
        "verdict",
        "Agrees with the differential",
    )
    assert msg.data == {}


def test_verdict_for_prefers_the_target_specific_verdict_over_the_case_level_one():
    """Synthesis must attach each hypothesis's own verdict, not the blanket case verdict."""
    state = CaseState(
        patient_id="p1",
        presenting_complaint="Fever",
        verifier_verdicts=[
            VerifierVerdict(target="case", status="agree", rationale="case-level"),
            VerifierVerdict(
                target="Dengue fever",
                status="flag",
                rationale="Platelet trend unconfirmed",
                caveats=["Repeat CBC needed"],
            ),
        ],
    )

    verdict = _verdict_for(state, "dengue fever")  # matched case-insensitively

    assert verdict["status"] == "flag"
    assert verdict["rationale"] == "Platelet trend unconfirmed"
    assert verdict["caveats"] == ["Repeat CBC needed"]


def test_verdict_for_falls_back_to_the_case_verdict_then_to_the_state_status():
    state = CaseState(
        patient_id="p1",
        presenting_complaint="Fever",
        verifier_verdicts=[VerifierVerdict(target="case", status="flag", rationale="low data")],
    )
    assert _verdict_for(state, "Unverified hypothesis")["status"] == "flag"

    bare = CaseState(patient_id="p1", presenting_complaint="Fever", verifier_status="agree")
    assert _verdict_for(bare, "anything") == {"status": "agree", "caveats": []}
