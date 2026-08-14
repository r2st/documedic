"""What each agent node does when the model answers in the wrong shape.

Every agent reads its own output as a list of objects — ``result.get("hypotheses", [])`` and
then ``h.get("diagnosis_name")`` on each item. Nothing guarantees the model produced that. A
weak model asked for a list of objects will hand back a list of bare strings, an object where a
list belongs, or ``"high"`` where a 0..1 score belongs, and each of those turned the ``.get`` or
the ``float()`` into an exception that escaped the node.

That matters more than a dropped field because of where these nodes sit. ``graph.run_reasoning``
wraps none of them, so the exception failed the whole reasoning session; and the deterministic
fallback each agent carries for exactly this situation never ran, because the crash happens
*while parsing a response the model did answer*, past the point where "nothing usable came back"
would have routed to the offline path. The Verifier was hardened against this (see
``verifier._recognised``); these are the six nodes that were not.

The contract asserted throughout: malformed entries are dropped, well-formed entries beside them
survive, the deterministic floor is preserved, and the node still completes.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents import (
    cant_miss_sentinel,
    guideline_rag,
    investigation_strategist,
    triage_intake,
    verifier,
)
from app.agents import hypothesis_panel as panel
from app.agents.context import ReasoningContext
from app.agents.llm import LLMClient
from app.agents.state import CaseState, Hypothesis
from app.agents.util import as_float, objects


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
    payload: dict[str, Any] | None, **kwargs: Any
) -> tuple[ReasoningContext, list[tuple[str, dict]]]:
    """A context whose emitted events are captured. ``None`` payload means the model is down."""
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    client: LLMClient = _OfflineLLM() if payload is None else _CannedLLM(payload)
    return ReasoningContext(llm=client, verifier_llm=client, emit=emit, **kwargs), events


def _completed(events: list[tuple[str, dict]], agent: str) -> bool:
    """Did the node reach its own ``agent_complete``? A crash never does."""
    return any(e == "agent_complete" and d.get("agent") == agent for e, d in events)


# --------------------------------------------------------------------------- util helpers


@pytest.mark.parametrize(
    "value",
    [
        ["Angina", "GERD"],  # list of bare strings
        {"a": {"diagnosis_name": "Angina"}},  # object where a list belongs
        "Angina",  # a bare string
        None,  # field omitted entirely
        42,
        [None, 7, "x"],  # a list, but of nothing usable
    ],
)
def test_objects_drops_everything_that_is_not_a_dict(value: object):
    assert objects(value) == []


def test_objects_keeps_the_well_formed_entries_beside_the_malformed_ones():
    """The point of dropping rather than raising: a partly-bad response is still worth reading."""
    payload = [{"diagnosis_name": "Angina"}, "GERD", None, {"diagnosis_name": "Costochondritis"}]
    assert objects(payload) == [
        {"diagnosis_name": "Angina"},
        {"diagnosis_name": "Costochondritis"},
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.8, 0.8),
        ("0.8", 0.8),  # a number the model quoted
        (1, 1.0),
        ("high", 0.25),  # a band where a score belongs
        (None, 0.25),  # "I don't know"
        ("", 0.25),
        ({"score": 0.9}, 0.25),
        ([0.9], 0.25),
        (True, 0.25),  # a bool is a non-answer, not 1.0
        (False, 0.25),
        (float("nan"), 0.25),  # NaN defeats every threshold comparison downstream
        (float("inf"), 0.25),
        (float("-inf"), 0.25),
    ],
)
def test_as_float_falls_back_for_anything_that_is_not_a_usable_number(value: object, expected):
    assert as_float(value, 0.25) == expected


# --------------------------------------------------------------------------- triage / intake


async def test_triage_survives_a_band_where_the_info_gain_score_belongs():
    """``float("high")`` raised on the first action a clinician takes on a case.

    ``ReasoningService.start`` runs this node, so the failure had no session to retry against —
    the case could not be opened at all.
    """
    state = CaseState(patient_id="p1", presenting_complaint="Chest pain since morning")
    ctx, events = _ctx({"intake_complete": False, "info_gain_score": "high", "questions": []})

    await triage_intake.run(state, ctx)

    assert _completed(events, "triage_intake")
    # The prior score is kept rather than invented: an unreadable answer is not an answer.
    assert state.info_gain_score == 1.0


async def test_triage_survives_a_null_info_gain_score():
    state = CaseState(patient_id="p1", presenting_complaint="Fever for three days")
    state.info_gain_score = 0.6
    ctx, events = _ctx({"intake_complete": False, "info_gain_score": None, "questions": []})

    await triage_intake.run(state, ctx)

    assert _completed(events, "triage_intake")
    assert state.info_gain_score == 0.6


async def test_triage_keeps_the_well_formed_questions_and_drops_the_rest():
    """A list mixing objects and bare strings must yield the objects, not an exception."""
    state = CaseState(patient_id="p1", presenting_complaint="Cough with fever")
    ctx, events = _ctx(
        {
            "intake_complete": False,
            "info_gain_score": 0.7,
            "questions": [
                "How long has the cough been present?",  # bare string — unusable
                {"text": "Any breathlessness at rest?", "info_gain_score": 0.8},
                None,
                {"text": "Any weight loss?", "info_gain_score": "high"},  # bad score, good text
            ],
        }
    )

    await triage_intake.run(state, ctx)

    assert _completed(events, "triage_intake")
    texts = [q.text for q in state.intake_questions]
    assert texts == ["Any breathlessness at rest?", "Any weight loss?"]
    assert state.intake_questions[0].info_gain_score == 0.8
    # The unreadable per-question score falls back rather than dropping a usable question.
    assert state.intake_questions[1].info_gain_score == 0.5


async def test_triage_survives_an_object_where_the_question_list_belongs():
    """Iterating a dict yields its keys — bare strings, so the same AttributeError."""
    state = CaseState(patient_id="p1", presenting_complaint="Headache")
    ctx, events = _ctx({"intake_complete": True, "questions": {"q1": "How long?"}})

    await triage_intake.run(state, ctx)

    assert _completed(events, "triage_intake")
    assert state.intake_questions == []


async def test_triage_does_not_ask_the_same_question_twice_in_one_response():
    """A model that repeats itself within one answer must still yield one question.

    ``existing`` was seeded from prior rounds but never updated as questions were appended, so a
    repeated text inside a single response was added once per occurrence — the clinician saw the
    same question two or three times in the intake list.
    """
    state = CaseState(patient_id="p1", presenting_complaint="Abdominal pain")
    ctx, _ = _ctx(
        {
            "intake_complete": False,
            "questions": [
                {"text": "Where exactly is the pain?"},
                {"text": "Where exactly is the pain?"},
                {"text": "where exactly is the pain?"},  # differs only in case
            ],
        }
    )

    await triage_intake.run(state, ctx)

    assert [q.text for q in state.intake_questions] == ["Where exactly is the pain?"]


async def test_triage_coerces_a_non_string_question_type_and_rationale():
    """These are rendered as strings in the intake UI; an object must not reach the client."""
    state = CaseState(patient_id="p1", presenting_complaint="Dizziness")
    ctx, _ = _ctx(
        {
            "questions": [
                {
                    "text": "Any recent change in medication?",
                    "question_type": {"kind": "history"},
                    "rationale": ["Drug causes are common", "and reversible"],
                }
            ]
        }
    )

    await triage_intake.run(state, ctx)

    q = state.intake_questions[0]
    assert isinstance(q.question_type, str) and q.question_type
    assert isinstance(q.rationale, str)
    assert "reversible" in q.rationale


# --------------------------------------------------------------------------- can't-miss sentinel


async def test_sentinel_keeps_its_deterministic_rules_when_the_model_answers_badly():
    """The rule table is the offline safety floor and must outlive a malformed augmentation.

    The deterministic matches are appended before the LLM section runs, so an exception there
    discarded diagnoses that had already been found — by code that never called the model.
    """
    state = CaseState(
        patient_id="p1",
        presenting_complaint="Crushing chest pain radiating to the left arm with sweating",
    )
    ctx, events = _ctx({"cant_miss": ["Myocardial infarction", "Aortic dissection"]})

    await cant_miss_sentinel.run(state, ctx)

    assert _completed(events, "cant_miss_sentinel")
    # The rule table matched this presentation; those hypotheses must still be on the list.
    assert state.hypothesis_set, "the deterministic can't-miss floor was lost"
    assert all(h.cant_miss_flag for h in state.hypothesis_set)


async def test_sentinel_reads_a_well_formed_entry_beside_a_malformed_one():
    state = CaseState(patient_id="p1", presenting_complaint="Sudden severe headache")
    ctx, _ = _ctx(
        {
            "cant_miss": [
                "Subarachnoid haemorrhage",  # bare string — unusable
                {
                    "diagnosis_name": "Bacterial meningitis",
                    "why_dangerous": "Rapidly fatal without early antibiotics.",
                    "evidence_for": [{"text": "Sudden onset with neck stiffness"}],
                },
            ]
        }
    )

    await cant_miss_sentinel.run(state, ctx)

    added = [h for h in state.hypothesis_set if h.diagnosis_name == "Bacterial meningitis"]
    assert len(added) == 1
    assert added[0].cant_miss_flag is True
    assert added[0].evidence_for[0].text == "Sudden onset with neck stiffness"


async def test_sentinel_falls_back_to_the_danger_text_when_evidence_items_are_strings():
    """A can't-miss diagnosis must never be added with no evidence attached to it."""
    state = CaseState(patient_id="p1", presenting_complaint="Breathlessness")
    ctx, _ = _ctx(
        {
            "cant_miss": [
                {
                    "diagnosis_name": "Pulmonary embolism",
                    "why_dangerous": "High early mortality if untreated.",
                    "evidence_for": ["sudden onset", "pleuritic pain"],  # strings, not objects
                }
            ]
        }
    )

    await cant_miss_sentinel.run(state, ctx)

    pe = next(h for h in state.hypothesis_set if h.diagnosis_name == "Pulmonary embolism")
    assert [e.text for e in pe.evidence_for] == ["High early mortality if untreated."]


async def test_sentinel_drops_a_non_string_icd_code_rather_than_carrying_it():
    state = CaseState(patient_id="p1", presenting_complaint="Fever")
    ctx, _ = _ctx(
        {
            "cant_miss": [
                {"diagnosis_name": "Sepsis", "icd_code": {"code": "A41"}, "why_dangerous": "x"}
            ]
        }
    )

    await cant_miss_sentinel.run(state, ctx)

    sepsis = next(h for h in state.hypothesis_set if h.diagnosis_name == "Sepsis")
    assert sepsis.icd_code is None


# --------------------------------------------------------------------------- hypothesis panel


async def test_panel_falls_back_to_the_deterministic_differential_on_a_malformed_response():
    """Four specialists run under ``asyncio.gather``; one bad list took down all four.

    With ``produced`` never populated and the exception propagating out of the gather, the
    deterministic panel that exists for this did not run either — the differential was empty.
    """
    state = CaseState(patient_id="p1", presenting_complaint="Fever with cough and sputum")
    ctx, events = _ctx({"hypotheses": ["Community-acquired pneumonia", "Bronchitis"]})

    await panel.run(state, ctx)

    assert _completed(events, "hypothesis_panel")
    assert state.hypothesis_set, "the differential was left empty by a malformed response"
    # Nothing usable came back, so the case is degraded — and says so.
    assert state.degraded is True


async def test_panel_keeps_the_well_formed_hypotheses_from_a_mixed_list():
    state = CaseState(patient_id="p1", presenting_complaint="Chest pain")
    ctx, _ = _ctx(
        {
            "hypotheses": [
                "Angina",  # bare string — unusable
                {"diagnosis_name": "Gastro-oesophageal reflux", "probability_band": "moderate"},
                None,
            ]
        }
    )

    await panel.run(state, ctx)

    names = {h.diagnosis_name for h in state.hypothesis_set}
    assert "Gastro-oesophageal reflux" in names
    # A response that yielded something is not degraded.
    assert state.degraded is False


async def test_panel_survives_an_object_where_the_hypothesis_list_belongs():
    state = CaseState(patient_id="p1", presenting_complaint="Joint pain and stiffness")
    ctx, events = _ctx({"hypotheses": {"first": {"diagnosis_name": "Inflammatory arthritis"}}})

    await panel.run(state, ctx)

    assert _completed(events, "hypothesis_panel")
    assert state.hypothesis_set
    assert state.degraded is True


async def test_panel_drops_a_non_string_icd_code():
    """``icd_code`` was passed through unchecked and is rendered on the suggestion card."""
    state = CaseState(patient_id="p1", presenting_complaint="Cough")
    ctx, _ = _ctx(
        {"hypotheses": [{"diagnosis_name": "Pulmonary tuberculosis", "icd_code": ["A15", "A16"]}]}
    )

    await panel.run(state, ctx)

    tb = next(h for h in state.hypothesis_set if h.diagnosis_name == "Pulmonary tuberculosis")
    assert tb.icd_code is None


# --------------------------------------------------------------------------- strategist


async def test_strategist_falls_back_when_the_investigation_list_is_bare_strings():
    state = CaseState(
        patient_id="p1",
        presenting_complaint="Chest pain",
        hypothesis_set=[Hypothesis(diagnosis_name="Angina", probability_band="moderate")],
    )
    ctx, events = _ctx({"investigations": ["ECG", "Troponin"]})

    await investigation_strategist.run(state, ctx)

    assert _completed(events, "investigation_strategist")
    # Nothing usable parsed, so the deterministic discriminating-test computation runs.
    assert state.recommended_investigations


async def test_strategist_coerces_a_non_string_rationale_and_cost():
    """Both are rendered as strings on the investigation card."""
    state = CaseState(patient_id="p1", presenting_complaint="Fever")
    ctx, _ = _ctx(
        {
            "investigations": [
                {
                    "name": "Blood culture",
                    "rationale": {"text": "Identifies the organism before antibiotics."},
                    "cost_estimate": {"inr": 800},
                    "availability_tier": "not_a_tier",
                }
            ]
        }
    )

    await investigation_strategist.run(state, ctx)

    inv = state.recommended_investigations[0]
    assert inv.rationale == "Identifies the organism before antibiotics."
    assert isinstance(inv.cost_estimate, str)
    # An unrecognised tier falls back to the most widely available one.
    assert inv.availability_tier == "phc"


# --------------------------------------------------------------------------- guideline RAG


def _chunk(section_id: str = "STW-HTN-1", **overrides: Any) -> dict[str, Any]:
    base = {
        "section_id": section_id,
        "source": "icmr",
        "document_title": "ICMR STW: Hypertension",
        "heading": "First-line management",
        "content": "Lifestyle modification is advised alongside pharmacological therapy.",
        "score": 0.92,
        "corpus_version": "v1",
        "page_range": "12-13",
    }
    base.update(overrides)
    return base


async def test_rag_survives_a_retrieved_chunk_with_no_section_id():
    """``ctx.retrieve`` is an injection point — in production a Qdrant payload, not app code.

    A chunk with no ``section_id`` raised KeyError building the citation map, failing the case
    over a payload defect that should cost one citation. Run with the model offline so what is
    under test is the retriever payload, not the model's answer.
    """
    state = CaseState(patient_id="p1", presenting_complaint="High blood pressure")
    ctx, events = _ctx(None)
    ctx.retrieve = lambda q, k: [{"content": "text", "score": 0.9}, _chunk()]

    await guideline_rag.run(state, ctx)

    assert _completed(events, "guideline_rag")
    # The usable chunk still grounded an option; the unusable one was dropped.
    assert state.management_options
    assert state.management_options[0].citations[0].section_id == "STW-HTN-1"


async def test_rag_survives_a_non_numeric_chunk_score():
    state = CaseState(patient_id="p1", presenting_complaint="High blood pressure")
    ctx, events = _ctx(None)
    ctx.retrieve = lambda q, k: [_chunk("STW-BAD", score="high"), _chunk()]

    await guideline_rag.run(state, ctx)

    assert _completed(events, "guideline_rag")
    cited = {c.section_id for o in state.management_options for c in o.citations}
    # "high" is not a score, so that chunk cannot clear the retrieval threshold.
    assert cited == {"STW-HTN-1"}


async def test_rag_survives_management_options_that_are_bare_strings():
    """The model's own options list is as untrusted as any other field.

    An answer nothing survives parsing from is not an answer of "nothing": the deterministic
    grounding must still present the retrieved chunks, or the clinician is shown no management
    options from a corpus that was already retrieved and scored.
    """
    state = CaseState(patient_id="p1", presenting_complaint="High blood pressure")
    ctx, events = _ctx({"options": ["Consider amlodipine 5mg", "Consider lifestyle advice"]})
    ctx.retrieve = lambda q, k: [_chunk()]

    await guideline_rag.run(state, ctx)

    assert _completed(events, "guideline_rag")
    assert state.management_options
    assert state.management_options[0].citations[0].section_id == "STW-HTN-1"
    assert state.degraded is True


async def test_rag_respects_a_model_that_deliberately_offered_no_options():
    """The converse: an empty options field is a finding, not a parse failure.

    Overriding it with raw chunks would contradict the model's own insufficient-support
    conclusion, so the deterministic grounding must stay out of the way here.
    """
    state = CaseState(patient_id="p1", presenting_complaint="High blood pressure")
    ctx, events = _ctx({"options": [], "insufficient_support": True})
    ctx.retrieve = lambda q, k: [_chunk()]

    await guideline_rag.run(state, ctx)

    assert _completed(events, "guideline_rag")
    assert state.management_options == []
    assert state.degraded is False
    insufficient = [d for e, d in events if e == "management"]
    assert insufficient and insufficient[0]["insufficient_support"] is True


async def test_rag_ignores_an_unhashable_citation_id():
    """``sid in refs`` raises TypeError on a list — in the node that grounds every option."""
    state = CaseState(patient_id="p1", presenting_complaint="High blood pressure")
    ctx, events = _ctx(
        {
            "options": [
                {
                    "text": "Guidelines support considering lifestyle modification.",
                    "citation_section_ids": [["STW-HTN-1"], {"id": "STW-HTN-1"}, "STW-HTN-1"],
                }
            ]
        }
    )
    ctx.retrieve = lambda q, k: [_chunk()]

    await guideline_rag.run(state, ctx)

    assert _completed(events, "guideline_rag")
    opt = state.management_options[0]
    assert [c.section_id for c in opt.citations] == ["STW-HTN-1"]


async def test_rag_survives_a_string_where_the_citation_id_list_belongs():
    state = CaseState(patient_id="p1", presenting_complaint="High blood pressure")
    ctx, _ = _ctx(
        {
            "options": [
                {
                    "text": "Guidelines support considering salt restriction.",
                    "citation_section_ids": "STW-HTN-1",
                }
            ]
        }
    )
    ctx.retrieve = lambda q, k: [_chunk()]

    await guideline_rag.run(state, ctx)

    opt = state.management_options[0]
    # An uncited option is kept but marked unsupported rather than silently credited to a
    # guideline it never cited — iterating the string would have matched nothing anyway.
    assert opt.citations == []
    assert state.citation_faithfulness == 1.0


# --------------------------------------------------------------------------- the whole pipeline


async def test_a_malformed_response_from_every_agent_still_produces_a_verified_case():
    """The end-to-end contract: a uniformly bad model degrades, it does not fail the session.

    One payload is served to every node, and it is the wrong shape for all of them. The run must
    still reach synthesis through the Verifier, and the Verifier must escalate — a case reasoned
    in degraded mode is flag-for-review (Critical Safety Rules #1, #2 and the degraded floor).
    """
    from app.agents import graph

    malformed = {
        "hypotheses": ["Angina"],
        "cant_miss": ["Myocardial infarction"],
        "investigations": ["ECG"],
        "options": ["Consider aspirin"],
        "status": ["agree"],
        "autonomy_tier": {"tier": "informational"},
        "verdicts": "all good",
        "case_caveats": None,
        "info_gain_score": "high",
        "questions": ["How long?"],
    }
    state = CaseState(
        patient_id="p1",
        presenting_complaint="Crushing chest pain radiating to the left arm since 6am",
        intake_complete=True,
    )
    ctx, events = _ctx(malformed)
    ctx.retrieve = lambda q, k: [_chunk()]

    output = await graph.run_reasoning(state, ctx)

    assert _completed(events, "verifier"), "the mandatory verifier gate did not run"
    assert _completed(events, "synthesis")
    assert output["suggestions"], "the clinician would have been shown nothing"
    assert state.degraded is True
    # Conservative wins: a degraded run cannot be presented as informational.
    assert state.autonomy_tier == "flag_for_review"


async def test_the_verifier_still_holds_the_floor_when_its_own_response_is_malformed():
    """Regression guard on the node this hardening pattern came from."""
    state = CaseState(
        patient_id="p1",
        presenting_complaint="Chest pain",
        hypothesis_set=[Hypothesis(diagnosis_name="ACS", cant_miss_flag=True)],
    )
    ctx, events = _ctx(
        {"status": {"verdict": "agree"}, "autonomy_tier": ["informational"], "verdicts": 7}
    )

    await verifier.run(state, ctx)

    assert _completed(events, "verifier")
    # The LLM could not lower the deterministic floor a can't-miss diagnosis sets.
    assert state.autonomy_tier == "flag_for_review"
    assert state.verifier_verdicts
