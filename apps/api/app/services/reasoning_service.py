"""ReasoningService — orchestrates a reasoning session over the multi-agent engine (P2).

Drives the human-in-the-loop intake loop, runs the pipeline, and persists every clinical output
as an IMMUTABLE ``ClinicalSuggestion`` (Critical Safety Rule #7). Provides both a blocking
``run`` and an SSE ``stream`` that relays Reasoning Theatre events live. All clinical output is
produced by ``graph.run_reasoning``, which always routes through the Verifier (Rule #1).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import graph
from app.agents.context import EventEmitter, ReasoningContext, Retriever, SafetyEvaluator
from app.agents.llm import LLMClient, is_available
from app.agents.state import CaseState, IntakeQuestionState
from app.config import settings
from app.core.logsafe import describe_exception
from app.exceptions import NotFoundError, ReasoningRunInProgressError, ValidationError
from app.models.clinical_suggestion import ClinicalSuggestion, ClinicianDecisionRecord
from app.models.intake import IntakeAnswer, IntakeQuestion
from app.models.patient import Patient
from app.models.reasoning_session import ReasoningSession
from app.services.audit_service import AuditDraft, AuditService
from app.services.guideline_service import GuidelineService
from app.services.record_service import REASONING_SNAPSHOT_LIMIT, RecordService
from app.services.safety_service import SafetyFlag, SafetyService

logger = logging.getLogger(__name__)

# The one status that means "a pipeline run holds this session".
RUNNING = "reasoning"


def _aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; treat a naive timestamp as UTC.

    Same helper, same reason, as ``app.services.auth_service._aware``.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


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

    async def _patient_for_processing(
        self, account_id: uuid.UUID, patient_id: uuid.UUID
    ) -> Patient:
        """:meth:`_patient` plus the DPDP lawful-basis check, for opening a session.

        Running the engine is processing personal data, so it needs consent to still be in
        force; reading a session's questions, answers and suggestions afterwards does not.
        See :class:`~app.exceptions.ConsentWithdrawnError`.
        """
        from app.services.patient_service import PatientService

        return await PatientService(self.db).get_for_processing(account_id, patient_id)

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
        """Freeze the patient graph into the dict the agents reason over.

        Takes ``REASONING_SNAPSHOT_LIMIT`` per section rather than the record endpoint's
        default: that default is sized for what a clinician can see on a chart, and what a
        differential is built from is not the same question. The bound is still deliberate --
        this is the read that used to have none -- and the dumped ``pagination`` travels into
        ``case_state`` with it, so a session run against a truncated chart says so in the
        immutable record of that session rather than looking like a complete one.
        """
        record = await self.records.assemble(patient_id, limit=REASONING_SNAPSHOT_LIMIT)
        return record.model_dump(mode="json")

    async def _safety_evaluator(
        self, account_id: uuid.UUID, patient_id: uuid.UUID
    ) -> SafetyEvaluator:
        """The deterministic drug-safety evaluator the reasoning graph calls.

        Answers two different questions through the one seam the graph has (see
        :data:`~app.agents.context.SafetyEvaluator`):

        * ``""`` — the patient's current medications checked against each other. Precomputed,
          because it is the same answer however many times the node asks.
        * any other text — the drugs *named in* it, checked against this patient. This is what
          screens a guideline management option before the clinician sees it. It cannot be
          precomputed: which contraindication and interaction rules matter depends on which
          drugs the text turns out to name, and that is not known until the retrieval and
          drafting upstream of the safety node have run.

        The second arm was for a long time the reason this signature took a drug name at all,
        and it returned the first arm's constant regardless — a seam that looked wired and was
        inert. Everything it now finds was reachable the whole time.
        """
        service = SafetyService(self.db)
        results = await service.active_flags(account_id=account_id, patient_id=patient_id)
        current: list[dict] = [_flag_dict(f) for _vocab, flag_list in results for f in flag_list]
        # Screening results memoised per text: management options are drafted from overlapping
        # guideline excerpts, so the same sentence is commonly screened more than once in a run,
        # and each miss costs a handful of queries on a path that is streaming to the clinician.
        screened: dict[str, list[dict]] = {}

        async def evaluate(text: str) -> list[dict]:
            if not text or not text.strip():
                return current
            if text not in screened:
                screened[text] = [
                    _flag_dict(f) for f in await service.screen_text(patient_id, text)
                ]
            return screened[text]

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
        # Pull the whole active corpus once; score in-memory. ``_load_corpus`` caches the
        # tokenised corpus per version behind a one-aggregate freshness check, so this is not a
        # full read per request — see GuidelineService._load_corpus, which this is the heaviest
        # caller of.
        #
        # The closure is async so the dense re-ranker's query embedding can be awaited off the
        # event loop. It used to be sync because the lexical retriever needs nothing but CPU and
        # the corpus in hand; a transformer forward pass on the loop that is streaming this
        # reasoning run's events to the clinician is a different proposition.
        from app.services.guideline_service import dense_scores, lexical_score

        chunks = await self.guidelines._load_corpus(None)  # noqa: SLF001 — internal reuse

        async def retrieve(query: str, k: int) -> list[dict]:
            return lexical_score(query, chunks, k, dense=await dense_scores(query, chunks))

        return retrieve

    # ------------------------------------------------------------------ intake
    async def start(
        self, account_id: uuid.UUID, patient_id: uuid.UUID, complaint: str
    ) -> tuple[ReasoningSession, list[IntakeQuestion]]:
        # Consent is checked here, at the one door into the engine, rather than on each
        # subsequent step: a session that was lawfully opened stays answerable and runnable, so
        # a withdrawal recorded mid-consultation does not strand the clinician half way through
        # an intake with no way to finish or to read the result. The next session is refused.
        await self._patient_for_processing(account_id, patient_id)
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
            # No presenting_complaint. It is free text a clinician typed about a patient —
            # "Ramesh, 54M, crushing chest pain since 6am" is a realistic value, and unlike a
            # marker name or a count it can carry a direct identifier. audit_logs.payload is
            # unencrypted, immutable and never pruned, so a name written here outlives the
            # patients row it was copied from and cannot be corrected or erased on a DPDP
            # request. The complaint is on reasoning_sessions, which entity_id points at.
            # Same reasoning as document_uploaded and file_name.
            payload={"online": online, "complaint_chars": len(complaint)},
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
        # One answer of record per question: a second answer rewrites the first rather than
        # adding a row beside it.
        #
        # Answers used to be appended unconditionally, and ``_rebuild_intake_state`` collapsed
        # them with ``answers_by_q[a.question_id] = a.answer_text`` over an unordered query — so
        # with two rows for one question, which one reached the engine was whatever order the
        # database returned. That is clinical *input*: ``agents.util.text_blob`` feeds
        # affirmative answers to the can't-miss sentinel and drops the keywords of anything
        # answered "no", so a ``red_flag`` answer decides whether a time-critical diagnosis is
        # screened in or out, and a clinician correcting "yes" to "no" could be silently ignored.
        #
        # Three ordinary things produce a second answer: a correction, a retried or
        # double-clicked submission, and one payload carrying the same question_id twice
        # (``SubmitAnswersRequest`` validates a list, not a set). Resolving them by "latest wins"
        # would need a tiebreak the schema cannot supply — rows written in one transaction share
        # a ``created_at``, and the primary key is a random UUID rather than a sequence — so the
        # duplicate is not created in the first place. Last answer wins, and re-submitting the
        # same payload is idempotent.
        existing = {
            a.question_id: a
            for a in (
                await self.db.execute(
                    select(IntakeAnswer).where(IntakeAnswer.session_id == session_id)
                )
            )
            .scalars()
            .all()
        }
        now = datetime.now(UTC)
        for ans in answers:
            qid = ans["question_id"]
            question = q_map.get(qid)
            if question is None:
                continue
            prior = existing.get(qid)
            if prior is not None:
                prior.answer_text = ans["answer_text"]
            else:
                row = IntakeAnswer(
                    question_id=qid,
                    session_id=session_id,
                    answer_text=ans["answer_text"],
                )
                self.db.add(row)
                # Registered before the flush so a repeat of the same question later in *this*
                # payload updates this row instead of adding a third.
                existing[qid] = row
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

    # --------------------------------------------------------------- the single-run claim
    def _claim_is_live(self, session: ReasoningSession) -> bool:
        """Whether a run currently holds this session.

        ``reasoning`` alone is not enough to answer. A run that was killed rather than finished
        leaves the status behind with nothing to clear it — and that is the ordinary case, not a
        rare one: the Reasoning Theatre streams over SSE, and a browser tab closing cancels the
        worker task with a ``CancelledError`` that the failure handler (which catches
        ``Exception``) never sees. So a claim is live only while its lease holds. Past that it is
        abandoned, and refusing to run because of it would make the case permanently unrunnable
        by a clinician who did nothing wrong.

        A NULL ``run_claimed_at`` under a ``reasoning`` status is a session from before the
        column existed, or one whose claim predates it. Treated as abandoned, which is the
        forgiving reading and the only safe one — there is no timestamp to argue otherwise.
        """
        if session.status != RUNNING:
            return False
        if session.run_claimed_at is None:
            return False
        lease = timedelta(minutes=settings.reasoning_run_lease_minutes)
        return _aware(session.run_claimed_at) + lease > datetime.now(UTC)

    def assert_runnable(self, session: ReasoningSession) -> None:
        """Refuse early if a run already holds this session. Advisory, not the guarantee.

        The guarantee is the compare-and-swap in :meth:`_claim_for_run`; this is a read of the
        row a caller has already loaded, so it can be wrong in the microseconds between the read
        and the claim. It exists for the SSE route, which cannot produce a status code once it
        has started streaming — and the status code is what matters there. A browser's
        ``EventSource`` treats a non-2xx response as a permanent failure and stops, but treats a
        200 stream that ends in an error as a transport problem and *reconnects*. Relaying the
        conflict inside the stream would therefore turn a second tab into the reconnect loop the
        reasoning rate limit exists to survive.
        """
        if self._claim_is_live(session):
            raise ReasoningRunInProgressError()

    async def _claim_for_run(self, session: ReasoningSession) -> None:
        """Take exclusive hold of the session for one pipeline run, or refuse.

        Why a claim and not the rate limiter: the limiter counts requests per minute and cannot
        stop two of them being *in flight*. Two concurrent runs each write a complete set of
        ClinicalSuggestion rows against the same ``session_id``. Those rows are immutable by
        database trigger (Rule #7), so the duplicates can never be removed — and the session
        header they hang under (status, autonomy_tier, case_state) is last-write-wins, so a case
        could end up reading ``suggestive`` above a hard-blocked suggestion the other run wrote.
        The clinician then has two overlapping differentials and nothing saying which is current.

        The atomicity is in the WHERE clause. Both the status and the claim timestamp must still
        be what this transaction read, so a racing claimant re-evaluates against the winner's
        committed row and matches nothing. ``run_claimed_at`` is written from Python rather than
        by ``func.now()`` precisely so it always moves: SQLite's clock has second resolution, and
        two takeovers inside one second would otherwise swap on an unchanged value and both win.

        The commit is not optional. A flush is invisible to the other request's transaction under
        READ COMMITTED, so an uncommitted claim would let both runs through — the claim has to be
        durable before the panel starts, which is also what leaves it behind for the lease to
        clean up if this run is killed.
        """
        if self._claim_is_live(session):
            raise ReasoningRunInProgressError()
        if session.status == RUNNING:
            logger.warning(
                "Reasoning session %s was left claimed by a run that did not finish; the lease "
                "has expired and it is being taken over.",
                session.id,
            )

        claimed_at = datetime.now(UTC)
        # CursorResult, not Result: only the former carries ``rowcount``, and the win/lose answer
        # is exactly "did the WHERE clause still match". ``Session.execute`` is typed as the
        # base, so the narrowing is spelled out rather than left to an ``attr-defined`` ignore.
        result = cast(
            "CursorResult[Any]",
            await self.db.execute(
                update(ReasoningSession)
                .where(
                    ReasoningSession.id == session.id,
                    ReasoningSession.status == session.status,
                    ReasoningSession.run_claimed_at == session.run_claimed_at,
                )
                .values(status=RUNNING, run_claimed_at=claimed_at, error_detail=None)
                # The ORM's default post-update synchronisation re-evaluates this WHERE clause
                # in Python against the identity map, which compares a tz-aware claim against
                # the naive datetime SQLite hands back and raises. Nothing here depends on that
                # sync: the two changed attributes are set on the instance below.
                .execution_options(synchronize_session=False)
            ),
        )
        await self.db.commit()
        if result.rowcount == 0:
            raise ReasoningRunInProgressError()
        session.status = RUNNING
        session.run_claimed_at = claimed_at

    # --------------------------------------------------------------- pipeline
    async def run(
        self, account_id: uuid.UUID, session_id: uuid.UUID, emit: EventEmitter | None = None
    ) -> tuple[ReasoningSession, list[ClinicalSuggestion]]:
        session = await self._session(account_id, session_id)
        # Exclusive from here: nothing else may run this session until the claim is released or
        # its lease expires. Taken before the snapshot is assembled so a losing caller spends one
        # UPDATE rather than a chart read.
        await self._claim_for_run(session)
        # When the LLM is unavailable the pipeline still runs deterministically (degraded mode):
        # it marks the case degraded, escalates to flag-for-review, and attaches an explicit
        # caveat rather than silently producing confident output.
        state = await self._rebuild_intake_state(session)
        state.intake_complete = True
        await self.db.flush()

        ctx = await self._build_context(account_id, session.patient_id, emit=emit)
        try:
            output = await graph.run_reasoning(state, ctx)
        except Exception as exc:  # noqa: BLE001 — record failure, surface to clinician
            session.status = "failed"
            # Both of these took str(exc). Exceptions here come back from an LLM provider that
            # was just sent this patient's snapshot, and audit_logs.payload is unencrypted,
            # immutable and never pruned — so a provider that quoted the prompt in its error
            # wrote a piece of the chart into the one table that outlives the record.
            failure = describe_exception(exc)
            session.error_detail = failure
            await self.audit.record(
                action="reasoning_session_failed",
                account_id=account_id,
                patient_id=session.patient_id,
                entity_type="reasoning_session",
                entity_id=session.id,
                payload={"error": failure},
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

        closing = [
            AuditDraft(
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
        ]
        if state.hard_blocks:
            closing.append(
                AuditDraft(
                    action="hard_block_triggered",
                    account_id=account_id,
                    patient_id=session.patient_id,
                    entity_type="reasoning_session",
                    entity_id=session.id,
                    payload={"hard_blocks": [b.summary for b in state.hard_blocks]},
                )
            )
        await self.audit.record_many(closing)
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
        # One batched append rather than one per suggestion. A run emits a suggestion per
        # differential, per can't-miss finding, per investigation and per management option,
        # so appending them individually made the audit cost — and the time the global append
        # lock is held — grow with how much the engine had to say.
        await self.audit.record_many(
            [
                AuditDraft(
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
                for row in rows
            ]
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
                # app.main.unhandled_error_handler keeps exception text out of every HTTP
                # response body. SSE bypasses it: this shipped str(exc) to the browser
                # verbatim, internals and whatever the provider echoed along with them.
                await queue.put(("error", {"message": describe_exception(exc)}))
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
            # The reasoning itself stays on clinician_decision_records, which this points at.
            # It is free text about a patient and audit_logs.payload is unencrypted, immutable
            # and never pruned — see reasoning_session_started. What the trail has to show is
            # that reasoning *was* documented when the tier required it, which is a boolean.
            payload={
                "decision": decision,
                "decision_record_id": str(record.id),
                "reason_recorded": bool(reason and reason.strip()),
            },
        )
        await self.db.commit()
        return record


def _flag_dict(flag: SafetyFlag) -> dict:
    """A deterministic safety flag in the shape the reasoning graph and SSE stream carry."""
    return {
        "check_type": flag.check_type,
        "severity": flag.severity,
        "is_hard_block": flag.is_hard_block,
        "summary": flag.summary,
        "details": flag.details,
    }
