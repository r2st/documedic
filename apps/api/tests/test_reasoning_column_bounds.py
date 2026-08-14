"""Model-derived values that reach a length-bounded column on the reasoning path.

R28/R29 put column fitting in front of the *merge* — the extraction path — because the default
test database is in-memory SQLite, which enforces no ``VARCHAR(n)`` length, so an overflow stays
green here and 500s on the PostgreSQL every deployment runs on. The reasoning path writes to
columns too, and was never swept the same way.

``IntakeQuestion.question_type`` is ``String(30)`` and was written straight from the model's
answer. ``prompts.TRIAGE_INTAKE`` asks for one of five short values, but a model that answers
"red_flag_screening_question_for_acs" (35 characters) is not misbehaving in any way this code
would otherwise notice — and that write happens inside ``ReasoningService.start``, the first
thing that runs when a clinician opens a case, so the failure leaves them unable to open it at
all with no session to retry against.

The sweep at the bottom is the durable part: it drives a whole session with a model answering
badly at every node and asserts every row that reaches the database would survive a typed one.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.agents import triage_intake
from app.agents.context import ReasoningContext
from app.agents.llm import LLMClient
from app.agents.state import CaseState
from app.agents.triage_intake import _DEFAULT_QUESTION_TYPE, _QUESTION_TYPES, _question_type
from app.models.clinical_suggestion import ClinicalSuggestion
from app.models.intake import IntakeAnswer, IntakeQuestion
from app.models.reasoning_session import ReasoningSession
from tests.column_fit import column_fit_violations
from tests.conftest import create_patient

_QUESTION_TYPE_LIMIT = IntakeQuestion.__table__.c.question_type.type.length


class _CannedLLM(LLMClient):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        return self.payload


async def _apply_question_type(raw: object) -> str:
    """Run one triage round with ``question_type`` set to ``raw``; return what was stored."""
    state = CaseState(patient_id="p1", presenting_complaint="Chest pain since this morning")
    client = _CannedLLM(
        {"questions": [{"text": "Any radiation to the jaw?", "question_type": raw}]}
    )
    await triage_intake.run(state, ReasoningContext(llm=client, verifier_llm=client))
    return state.intake_questions[0].question_type


# ------------------------------------------------------------------ the value itself


def test_every_question_type_the_prompt_asks_for_fits_the_column():
    """If a permitted value did not fit, normalising to the set would not save the write."""
    assert _QUESTION_TYPES, "the permitted set must not be empty"
    for kind in _QUESTION_TYPES:
        assert len(kind) <= _QUESTION_TYPE_LIMIT, (
            f"{kind!r} exceeds VARCHAR({_QUESTION_TYPE_LIMIT})"
        )
    assert _DEFAULT_QUESTION_TYPE in _QUESTION_TYPES


def test_the_deterministic_fallback_only_uses_permitted_question_types():
    """The offline intake path builds these itself; it must agree with the same closed set."""
    state = CaseState(patient_id="p1", presenting_complaint="Chest pain with sweating")
    triage_intake._fallback(state)

    assert state.intake_questions
    for question in state.intake_questions:
        assert question.question_type in _QUESTION_TYPES


@pytest.mark.parametrize("kind", sorted(_QUESTION_TYPES))
def test_a_permitted_question_type_is_passed_through(kind: str):
    assert _question_type(kind) == kind


@pytest.mark.parametrize(
    "raw",
    [
        "red_flag_screening_question_for_acs",  # 35 chars — plausible, and one past the column
        "R" * 200,
        "clarifying question about onset and radiation",
        "unknown_kind",
        "",
        "   ",
        None,
        42,
        {"kind": "red_flag"},
        ["red_flag"],
    ],
)
async def test_a_stored_question_type_is_always_one_the_column_and_the_ui_know(raw: object):
    """The invariant, whatever the model says: a value from the closed permitted set.

    Some of these do land on a real type — ``as_text`` flattens ``{"kind": "red_flag"}`` to
    ``"red_flag"``, which is a correct reading of a model that nested its answer. What must
    never happen is a value outside the set reaching the column.
    """
    assert await _apply_question_type(raw) in _QUESTION_TYPES


@pytest.mark.parametrize(
    "raw",
    [
        "red_flag_screening_question_for_acs",  # 35 chars — plausible, and one past the column
        "R" * 200,
        "clarifying question about onset and radiation",
        "unknown_kind",
        "",
        None,
        42,
    ],
)
async def test_an_unmappable_question_type_becomes_the_default(raw: object):
    assert await _apply_question_type(raw) == _DEFAULT_QUESTION_TYPE


@pytest.mark.parametrize("raw", ["RED_FLAG", "Red_Flag", "  red_flag  "])
async def test_a_permitted_question_type_is_recognised_despite_case_or_padding(raw: str):
    """A model that shouts or pads is answering correctly; it should not be normalised away."""
    assert await _apply_question_type(raw) == "red_flag"


async def test_the_stored_question_type_always_fits_its_column():
    """The property that actually matters, asserted against the mapped column."""
    for raw in ["red_flag_screening_question_for_acs", "R" * 500, None, {"a": 1}, "history"]:
        stored = await _apply_question_type(raw)
        row = IntakeQuestion(question_text="q", question_type=stored, sequence_order=0)
        assert column_fit_violations(row) == []


# ------------------------------------------------------------------ whole-path sweep


@pytest.fixture
def _hostile_model(monkeypatch):
    """Every agent gets a reply that is both malformed and far too long for any column.

    Patched on the class because ``ReasoningService._build_context`` constructs its own
    ``LLMClient`` instances per request, so there is no single object to inject into.
    """
    long = "L" * 5000
    payload = {
        # triage
        "intake_complete": False,
        "info_gain_score": "high",
        "questions": [{"text": long, "question_type": long, "rationale": long}],
        # hypothesis panel / sentinel
        "hypotheses": [{"diagnosis_name": long, "probability_band": long, "icd_code": long}],
        "cant_miss": [{"diagnosis_name": long, "why_dangerous": long}],
        # strategist
        "investigations": [{"name": long, "rationale": long, "availability_tier": long}],
        # guideline RAG
        "options": [{"text": long, "citation_section_ids": [long]}],
        # verifier
        "status": long,
        "autonomy_tier": long,
        "verdicts": [{"target": long, "status": long, "rationale": long, "caveats": [long]}],
        "case_caveats": [long],
    }
    monkeypatch.setattr(LLMClient, "available", lambda self: True)
    monkeypatch.setattr(
        LLMClient,
        "complete_json",
        lambda self, system, user, retries=2: payload,
    )
    return payload


async def test_every_row_a_hostile_session_writes_would_survive_a_typed_database(
    auth_client, db, _hostile_model
):
    """Drive a full session through the API and sweep everything it persisted.

    This is the check the reasoning path never had. SQLite accepts all of it, so without the
    sweep an over-long value written anywhere on this path stays invisible until PostgreSQL
    rejects it in production.
    """
    patient = await create_patient(auth_client)

    started = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "Crushing chest pain radiating to the left arm"},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session"]["id"]

    for _ in range(4):
        pending = (await auth_client.get(f"/api/v1/reasoning/{session_id}/intake")).json()
        if not pending:
            break
        answered = await auth_client.post(
            f"/api/v1/reasoning/{session_id}/intake/answers",
            json={"answers": [{"question_id": q["id"], "answer_text": "no"} for q in pending]},
        )
        assert answered.status_code == 200, answered.text
        if answered.json()["intake_complete"]:
            break

    run = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert run.status_code == 200, run.text

    violations: list[str] = []
    for model in (ReasoningSession, IntakeQuestion, IntakeAnswer, ClinicalSuggestion):
        for row in (await db.execute(select(model))).scalars().all():
            violations.extend(column_fit_violations(row))

    assert violations == [], "\n".join(violations)


async def test_the_sweep_would_notice_an_over_long_question_type(db):
    """The sweep is only worth having if it fails on the value this file exists for."""
    row = IntakeQuestion(
        question_text="q", question_type="R" * (_QUESTION_TYPE_LIMIT + 1), sequence_order=0
    )

    violations = column_fit_violations(row)

    assert violations and "question_type" in violations[0]
