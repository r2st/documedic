"""ReasoningService — orchestrates a reasoning session over the multi-agent engine (P2).

Drives the human-in-the-loop intake loop, runs the pipeline, and persists every clinical output
as an IMMUTABLE ``ClinicalSuggestion`` (Critical Safety Rule #7). Provides both a blocking
``run`` and an SSE ``stream`` that relays Reasoning Theatre events live. All clinical output is
produced by ``graph.run_reasoning``, which always routes through the Verifier (Rule #1).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import graph
from app.agents.context import EventEmitter, ReasoningContext, Retriever, SafetyEvaluator
from app.agents.llm import LLMClient, is_available
from app.agents.state import CaseState, IntakeQuestionState
from app.config import settings
from app.exceptions import NotFoundError, ValidationError
from app.models.clinical_suggestion import ClinicalSuggestion, ClinicianDecisionRecord
from app.models.intake import IntakeAnswer, IntakeQuestion
from app.models.patient import Patient
from app.models.reasoning_session import ReasoningSession
from app.services.audit_service import AuditService
from app.services.guideline_service import GuidelineService
from app.services.record_service import RecordService
from app.services.safety_service import SafetyService


class ReasoningSessionNotFoundError(NotFoundError):
    """Reasoning session not found."""

    code = "reasoning_session_not_found"


class SuggestionNotFoundError(NotFoundError):
    """Clinical suggestion not found."""

    code = "suggestion_not_found"


class ReasoningService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)
        self.records = RecordService(db)
        self.guidelines = GuidelineService(db)

    # ----------------------------------------------------------------- helpers
    async def _patient(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
        """Fetch a patient the caller owns, or raise PatientNotFoundError.

        Delegates rather than repeating the predicate. This is an authorization check -- it is
        what stops one account reading another's records -- and it was previously copy-pasted
        into four services. Any future change to what "a patient this caller may read" means
        (an extra tenancy dimension, an account-status check) has to land in one place or it
        lands in three and misses the fourth.

        Imported inside the method: PatientService imports from this module's siblings, so a
        module-level import would close a cycle. Same pattern as DocumentService._get_patient.
        """
        from app.services.patient_service import PatientService

        return await PatientService(self.db).get(account_id, patient_id)

    async def _session(self, account_id: uuid.UUID, session_id: uuid.UUID) -> ReasoningSession:
        result = await self.db.execute(
            select(ReasoningSession).where(
                ReasoningSession.id == session_id,
                ReasoningSession.account_id == account_id,
                ReasoningSession.is_deleted.is_(False),
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise ReasoningSessionNotFoundError()
        return session

    async def _snapshot(self, patient_id: uuid.UUID) -> dict[str, Any]:
        record = await self.records.assemble(patient_id)
        return record.model_dump(mode="json")

    async def _safety_evaluator(
        self, account_id: uuid.UUID, patient_id: uuid.UUID
    ) -> SafetyEvaluator:
        """Precompute the patient's active safety flags into a constant evaluator callable."""
        results = await SafetyService(self.db).active_flags(
            account_id=account_id, patient_id=patient_id
        )
        flags: list[dict] = []
        for _vocab, flag_list in results:
            for f in flag_list:
                flags.append(
                    {
                        "check_type": f.check_type,
                        "severity": f.severity,
                        "is_hard_block": f.is_hard_block,
                        "summary": f.summary,
                        "details": f.details,
                    }
                )

        def evaluate(_drug: str) -> list[dict]:
            return flags

        return evaluate

    async def _build_context(
        self,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        emit: EventEmitter | None = None,
    ) -> ReasoningContext:
        ctx = ReasoningContext(
            llm=LLMClient(),
            verifier_llm=LLMClient(),
            evaluate_safety=await self._safety_evaluator(account_id, patient_id),
            info_gain_threshold=settings.reasoning_info_gain_threshold,
            question_cap=settings.reasoning_question_cap,
            retrieval_threshold=settings.guideline_retrieval_threshold,
        )
        # Pre-fetch guideline chunks so the (sync) retriever closure has no DB dependency.
        ctx.retrieve = await self._make_retriever()
        if emit is not None:
            ctx.emit = emit
        return ctx

    async def _make_retriever(self) -> Retriever:
        # Pull the whole active corpus once; score in-memory (the agent's retriever is sync and
        # has no DB/event-loop access).
        from app.services.guideline_service import lexical_score

        chunks = await self.guidelines._chunks(None)  # noqa: SLF001 — internal reuse

        def retrieve(query: str, k: int) -> list[dict]:
            return lexical_score(query, chunks, k)

        return retrieve

    # ------------------------------------------------------------------ intake
    async def start(
        self, account_id: uuid.UUID, patient_id: uuid.UUID, complaint: str
    ) -> tuple[ReasoningSession, list[IntakeQuestion]]:
        await self._patient(account_id, patient_id)
        online = is_available()
        session = ReasoningSession(
            patient_id=patient_id,
            account_id=account_id,
            presenting_complaint=complaint,
            status="intake",
            online=online,
            started_at=datetime.now(UTC),
        )
        self.db.add(session)
        await self.db.flush()

        state = CaseState(
            patient_id=str(patient_id),
            presenting_complaint=complaint,
            patient_graph_snapshot=await self._snapshot(patient_id),
            case_id=str(session.id),
            online=online,
        )
        ctx = await self._build_context(account_id, patient_id)
        await graph.run_triage_round(state, ctx)
        questions = await self._persist_questions(session, state)
        await self._sync_intake_state(session, state)

        await self.audit.record(
            action="reasoning_session_started",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="reasoning_session",
            entity_id=session.id,
            payload={"presenting_complaint": complaint, "online": online},
        )
        await self.db.commit()
        return session, questions

    async def submit_answers(
        self, account_id: uuid.UUID, session_id: uuid.UUID, answers: list[dict]
    ) -> tuple[ReasoningSession, list[IntakeQuestion]]:
        session = await self._session(account_id, session_id)
        if session.intake_complete:
            return session, []

        q_map = {q.id: q for q in await self._questions(session_id)}
        now = datetime.now(UTC)
        for ans in answers:
            qid = ans["question_id"]
            question = q_map.get(qid)
            if question is None:
                continue
            self.db.add(
                IntakeAnswer(
                    question_id=qid,
                    session_id=session_id,
                    answer_text=ans["answer_text"],
                )
            )
            question.answered_at = now
        await self.db.flush()

        # Rebuild CaseState with answered questions, run the next triage round.
        state = await self._rebuild_intake_state(session)
        ctx = await self._build_context(account_id, session.patient_id)
        await graph.run_triage_round(state, ctx)
        new_questions = await self._persist_questions(session, state)
        await self._sync_intake_state(session, state)

        await self.audit.record(
            action="reasoning_intake_answered",
            account_id=account_id,
            patient_id=session.patient_id,
            entity_type="reasoning_session",
            entity_id=session.id,
            payload={"answered": len(answers), "intake_complete": session.intake_complete},
        )
        await self.db.commit()
        return session, new_questions

    async def _questions(self, session_id: uuid.UUID) -> list[IntakeQuestion]:
        result = await self.db.execute(
            select(IntakeQuestion)
            .where(IntakeQuestion.session_id == session_id)
            .order_by(IntakeQuestion.sequence_order)
        )
        return list(result.scalars().all())

    async def pending_questions(self, session_id: uuid.UUID) -> list[IntakeQuestion]:
        return [q for q in await self._questions(session_id) if q.answered_at is None]

    async def _persist_questions(
        self, session: ReasoningSession, state: CaseState
    ) -> list[IntakeQuestion]:
        existing = {q.question_text.strip().lower() for q in await self._questions(session.id)}
        created: list[IntakeQuestion] = []
        next_order = len(existing)
        for q in state.intake_questions:
            if q.answer is not None or q.text.strip().lower() in existing:
                continue
            row = IntakeQuestion(
                session_id=session.id,
                question_text=q.text,
                question_type=q.question_type,
                rationale=q.rationale,
                sequence_order=next_order,
                info_gain_score=q.info_gain_score,
            )
            self.db.add(row)
            created.append(row)
            existing.add(q.text.strip().lower())
            next_order += 1
        await self.db.flush()
        return created

    async def _sync_intake_state(self, session: ReasoningSession, state: CaseState) -> None:
        session.info_gain_score = state.info_gain_score
        session.intake_complete = state.intake_complete
        session.status = "intake_complete" if state.intake_complete else "intake"
        await self.db.flush()

    async def _rebuild_intake_state(self, session: ReasoningSession) -> CaseState:
        questions = await self._questions(session.id)
        answers_result = await self.db.execute(
            select(IntakeAnswer).where(IntakeAnswer.session_id == session.id)
        )
        answers_by_q: dict[uuid.UUID, str] = {}
        for a in answers_result.scalars().all():
            answers_by_q[a.question_id] = a.answer_text

        state = CaseState(
            patient_id=str(session.patient_id),
            presenting_complaint=session.presenting_complaint,
            patient_graph_snapshot=await self._snapshot(session.patient_id),
            case_id=str(session.id),
            online=session.online,
            intake_rounds=1,
        )
        for q in questions:
            state.intake_questions.append(
                IntakeQuestionState(
                    text=q.question_text,
                    question_type=q.question_type,
                    rationale=q.rationale,
                    info_gain_score=q.info_gain_score or 0.5,
                    answer=answers_by_q.get(q.id),
                )
            )
        return state

    # --------------------------------------------------------------- pipeline
    async def run(
        self, account_id: uuid.UUID, session_id: uuid.UUID, emit: EventEmitter | None = None
    ) -> tuple[ReasoningSession, list[ClinicalSuggestion]]:
        session = await self._session(account_id, session_id)
        # When the LLM is unavailable the pipeline still runs deterministically (degraded mode):
        # it marks the case degraded, escalates to flag-for-review, and attaches an explicit
        # caveat rather than silently producing confident output.
        state = await self._rebuild_intake_state(session)
        state.intake_complete = True
        session.status = "reasoning"
        await self.db.flush()

        ctx = await self._build_context(account_id, session.patient_id, emit=emit)
        try:
            output = await graph.run_reasoning(state, ctx)
        except Exception as exc:  # noqa: BLE001 — record failure, surface to clinician
            session.status = "failed"
            session.error_detail = str(exc)
            await self.audit.record(
                action="reasoning_session_failed",
                account_id=account_id,
                patient_id=session.patient_id,
                entity_type="reasoning_session",
                entity_id=session.id,
                payload={"error": str(exc)},
            )
            await self.db.commit()
            raise

        suggestions = await self._persist_suggestions(account_id, session, state, output)
        session.case_state = output["case_state"]
        session.autonomy_tier = state.autonomy_tier
        session.status = (
            "awaiting_review" if state.autonomy_tier == "flag_for_review" else "completed"
        )
        session.completed_at = datetime.now(UTC)
        await self.db.flush()

        await self.audit.record(
            action="reasoning_session_completed",
            account_id=account_id,
            patient_id=session.patient_id,
            entity_type="reasoning_session",
            entity_id=session.id,
            payload={
                "autonomy_tier": state.autonomy_tier,
                "verifier_status": state.verifier_status,
                "degraded": state.degraded,
                "suggestions": len(suggestions),
                "hard_blocks": len(state.hard_blocks),
            },
        )
        if state.hard_blocks:
            await self.audit.record(
                action="hard_block_triggered",
                account_id=account_id,
                patient_id=session.patient_id,
                entity_type="reasoning_session",
                entity_id=session.id,
                payload={"hard_blocks": [b.summary for b in state.hard_blocks]},
            )
        await self.db.commit()
        return session, suggestions

    async def _persist_suggestions(
        self,
        account_id: uuid.UUID,
        session: ReasoningSession,
        state: CaseState,
        output: dict,
    ) -> list[ClinicalSuggestion]:
        trace = output["case_state"].get("agent_trace", [])
        rows: list[ClinicalSuggestion] = []
        for s in output["suggestions"]:
            row = ClinicalSuggestion(
                session_id=session.id,
                patient_id=session.patient_id,
                output_type=s["output_type"],
                autonomy_tier=s["autonomy_tier"],
                confidence_band=s.get("confidence_band"),
                title=s["title"],
                body=s.get("body"),
                evidence=s.get("evidence", {}),
                citations=s.get("citations", []),
                agent_trace=trace,
                verifier_verdict=s.get("verifier_verdict", {}),
                devils_advocate=s.get("devils_advocate", {}),
                is_hard_block=bool(s.get("is_hard_block", False)),
                cant_miss_flag=bool(s.get("cant_miss_flag", False)),
            )
            self.db.add(row)
            rows.append(row)
        await self.db.flush()
        for row in rows:
            await self.audit.record(
                action="clinical_suggestion_created",
                account_id=account_id,
                patient_id=session.patient_id,
                entity_type="clinical_suggestion",
                entity_id=row.id,
                payload={
                    "output_type": row.output_type,
                    "autonomy_tier": row.autonomy_tier,
                    "title": row.title,
                    "is_hard_block": row.is_hard_block,
                    "cant_miss_flag": row.cant_miss_flag,
                },
            )
        return rows

    async def stream(
        self, account_id: uuid.UUID, session_id: uuid.UUID
    ) -> AsyncGenerator[tuple[str, dict], None]:
        """Async generator of (event, data) tuples for SSE, running the pipeline live."""
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        async def emit(event: str, data: dict) -> None:
            await queue.put((event, data))

        async def worker() -> None:
            try:
                await self.run(account_id, session_id, emit=emit)
            except Exception as exc:  # noqa: BLE001 — relay error to the stream
                await queue.put(("error", {"message": str(exc)}))
            finally:
                await queue.put(_SENTINEL)

        task = asyncio.create_task(worker())
        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                yield item
        finally:
            if not task.done():
                task.cancel()

    # --------------------------------------------------------------- queries
    async def get_session(self, account_id: uuid.UUID, session_id: uuid.UUID) -> ReasoningSession:
        return await self._session(account_id, session_id)

    async def list_suggestions(
        self, account_id: uuid.UUID, session_id: uuid.UUID
    ) -> list[ClinicalSuggestion]:
        await self._session(account_id, session_id)
        result = await self.db.execute(
            select(ClinicalSuggestion)
            .where(ClinicalSuggestion.session_id == session_id)
            .order_by(ClinicalSuggestion.created_at)
        )
        return list(result.scalars().all())

    async def record_decision(
        self,
        account_id: uuid.UUID,
        session_id: uuid.UUID,
        suggestion_id: uuid.UUID,
        decision: str,
        reason: str | None,
    ) -> ClinicianDecisionRecord:
        await self._session(account_id, session_id)
        suggestion = await self.db.get(ClinicalSuggestion, suggestion_id)
        if suggestion is None or suggestion.session_id != session_id:
            raise SuggestionNotFoundError()

        # Overriding a hard block or a flag-for-review requires documented reasoning (Rule #3).
        requires_reason = suggestion.is_hard_block or (
            suggestion.autonomy_tier == "flag_for_review" and decision == "overridden"
        )
        if requires_reason and not (reason and reason.strip()):
            raise ValidationError(
                "Overriding a hard block or flag-for-review requires documented reasoning."
            )

        record = ClinicianDecisionRecord(
            suggestion_id=suggestion_id,
            account_id=account_id,
            decision=decision,
            reason=reason,
        )
        self.db.add(record)
        await self.db.flush()
        await self.audit.record(
            action="clinician_decision_recorded",
            account_id=account_id,
            patient_id=suggestion.patient_id,
            entity_type="clinical_suggestion",
            entity_id=suggestion_id,
            payload={"decision": decision, "reason": reason},
        )
        await self.db.commit()
        return record
