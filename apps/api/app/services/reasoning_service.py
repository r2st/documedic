"""ReasoningService — orchestrates a reasoning session over the multi-agent engine (P2).

Drives the human-in-the-loop intake loop, runs the pipeline, and persists every clinical output
as an IMMUTABLE ``ClinicalSuggestion`` (Critical Safety Rule #7). Provides both a blocking
``run`` and an SSE ``stream`` that relays Reasoning Theatre events live. All clinical output is
produced by ``graph.run_reasoning``, which always routes through the Verifier (Rule #1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import graph, synthesis
from app.agents.context import EventEmitter, ReasoningContext, Retriever, SafetyEvaluator
from app.agents.graph import record_hard_block
from app.agents.llm import LLMClient, is_available
from app.agents.state import CaseState, IntakeQuestionState, more_conservative_tier
from app.config import settings
from app.core.logsafe import describe_exception
from app.exceptions import (
    ConcurrentAnswerError,
    NotFoundError,
    ReasoningRunInProgressError,
    ReasoningRunSupersededError,
    ValidationError,
)
from app.models.clinical_suggestion import ClinicalSuggestion, ClinicianDecisionRecord
from app.models.intake import IntakeAnswer, IntakeQuestion
from app.models.patient import Patient
from app.models.reasoning_session import RUNNING, ReasoningSession
from app.services.audit_service import AuditDraft, AuditService
from app.services.guideline_service import GuidelineService
from app.services.record_service import REASONING_SNAPSHOT_LIMIT, RecordService
from app.services.safety_service import SafetyFlag, SafetyService

logger = logging.getLogger(__name__)

# How many times ``submit_answers`` re-reads the stored answers after losing the insert race on
# ``uq_intake_answers_question``. Each retry only loses to a submission that succeeded, so the
# loop makes progress; the bound turns a pathological repeat into a 409 rather than a hung
# request. Same shape as ``AuditService._MAX_SEQUENCE_ATTEMPTS``, and much smaller because there
# is one contender per question rather than one per request in the whole system.
_ANSWER_WRITE_ATTEMPTS = 2

# Re-exported: the claim reads and writes this status, and it is defined next to the lease rule
# that interprets it. Importers of this module keep their existing spelling.
__all__ = ["RUNNING", "ReasoningService"]


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
        """The session, if this account owns it *and* its chart is still in use.

        The join to ``patients`` is the point. Every other patient-scoped route in the API
        reaches its rows through ``PatientService.get``, which filters ``is_deleted``, so
        withdrawing a chart at ``DELETE /patients/{id}`` turns the record, the safety flags,
        the documents and the chart itself into 404s. The reasoning routes reached their rows
        through the *session* id instead and never looked at the patient again after ``start``,
        so a session id kept from before the withdrawal stayed a working door into the chart:
        ``GET /reasoning/{id}`` returned ``case_state``, which for a completed run carries
        ``patient_graph_snapshot`` — a frozen copy of the whole longitudinal record, the exact
        payload ``GET /patients/{id}/record`` had just started refusing — and
        ``GET ../suggestions`` returned the engine's clinical output about the patient.

        Withdrawal is not erasure and this does not pretend otherwise: the rows survive, the
        immutable suggestions survive, and the audit trail stays readable through
        ``PatientService.get_for_audit``. What stops is the chart answering clinical questions
        about a patient it was told to stop being used for.

        Scoping the read here rather than at each of the eight routes because the alternative
        is eight places to remember — and the one that gets forgotten is the whole hole again.
        """
        result = await self.db.execute(
            select(ReasoningSession)
            .join(Patient, Patient.id == ReasoningSession.patient_id)
            .where(
                ReasoningSession.id == session_id,
                ReasoningSession.account_id == account_id,
                ReasoningSession.is_deleted.is_(False),
                Patient.is_deleted.is_(False),
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

        The first arm carries the chart-level flags too — what could not be evaluated, and how
        impaired this liver is — which is the same set ``GET /patients/{id}/drug-safety/flags``
        appends and for the same reason. Without them the Theatre's drug-safety panel reported
        the flags of a chart it could fully read, on a chart it could not: a medication line the
        vocabulary cannot resolve and an allergen it cannot identify are absent from every rule
        this arm evaluates, so an empty list there meant "nothing found" and "nothing checked"
        indistinguishably — on the one surface in the product built to show the clinician what
        the machine actually did. Appended once for the chart, not per drug, exactly as on the
        Safety screen; the per-option arm below is about drugs named in a sentence and gets
        none of them.
        """
        service = SafetyService(self.db)
        results = await service.active_flags(account_id=account_id, patient_id=patient_id)
        current: list[dict] = [_flag_dict(f) for _vocab, flag_list in results for f in flag_list]
        current += [_flag_dict(f) for f in await service.chart_completeness_flags(patient_id)]
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
        # One answer of record per question, and the loop is how a submission that *lost* the
        # race still lands. ``_record_answers`` decides between updating and inserting from a
        # read of the answers already stored, so two overlapping submissions both read no answer
        # to a question and both insert — the read-then-insert window that
        # ``uq_intake_answers_question`` now refuses. The loser's flush raises, its transaction
        # is rolled back, and it re-reads: the winner's row is committed by then, so the second
        # attempt takes the *update* path and the clinician's answer is applied as the correction
        # it was rather than dropped or written beside the other one.
        #
        # Bounded at two attempts. Each retry only loses to a submission that succeeded, so the
        # loop makes progress; the bound turns a pathological repeat into a 409 the client can
        # act on rather than a request that spins. See ``ConcurrentAnswerError`` for why the
        # duplicate must not simply be allowed.
        for remaining in reversed(range(_ANSWER_WRITE_ATTEMPTS)):
            if session.intake_complete:
                return session, []
            try:
                await self._record_answers(session_id, answers)
                break
            except IntegrityError as exc:
                await self.db.rollback()
                if not remaining:
                    raise ConcurrentAnswerError(detail=str(exc.orig)) from exc
                # The rollback expired every loaded object, this one included.
                session = await self._session(account_id, session_id)

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

    async def _record_answers(self, session_id: uuid.UUID, answers: list[dict]) -> None:
        """Write this submission's answers, one row of record per question.

        A second answer to a question rewrites the first rather than adding a row beside it.
        ``_rebuild_intake_state`` collapses answers with
        ``answers_by_q[a.question_id] = a.answer_text`` over an unordered query — so with two
        rows for one question, which one reaches the engine is whatever order the database
        returned. That is clinical *input*: ``agents.util.text_blob`` feeds affirmative answers
        to the can't-miss sentinel and drops the keywords of anything answered "no", so a
        ``red_flag`` answer decides whether a time-critical diagnosis is screened in or out, and
        a clinician correcting "yes" to "no" could be silently ignored.

        Three ordinary things produce a second answer: a correction, a retried or double-clicked
        submission, and one payload carrying the same question_id twice (``SubmitAnswersRequest``
        validates a list, not a set). Resolving them by "latest wins" would need a tiebreak the
        schema cannot supply — rows written in one transaction share a ``created_at``, and the
        primary key is a random UUID rather than a sequence — so the duplicate is not created in
        the first place. Last answer wins, and re-submitting the same payload is idempotent.

        Raises ``IntegrityError`` when a concurrent submission got there first; the caller
        re-reads and applies the answer over the winner's row. See :meth:`submit_answers`.
        """
        q_map = {q.id: q for q in await self._questions(session_id)}
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

        The rule itself is :attr:`ReasoningSession.run_in_progress`, and it is deliberately in
        one place: the same answer is serialized to clients, and a client told a run is in
        progress by one rule while the claim refuses or permits by another is worse than either
        rule being wrong. This wrapper exists so the refusal below reads as what it is.
        """
        return session.run_in_progress

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

    async def _claim_for_run(self, account_id: uuid.UUID, session: ReasoningSession) -> None:
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

        That durability is also why the audit entry for the run belongs here and nowhere else.
        It is the only commit a run makes before the panel starts, so it is the only one that
        survives a run that is killed rather than finished — and being killed is the ordinary
        case, not a rare one: the Reasoning Theatre streams over SSE and a closing tab cancels
        the worker. Everything ``run`` writes afterwards, including
        ``reasoning_session_completed``, is rolled back with that transaction. Audited from the
        claim, an abandoned run is legible in the trail as a ``reasoning_run_started`` with
        neither of its closing records ever arriving; audited any later it left no trace that a
        clinician had run the panel on this patient and watched its output stream back.
        """
        if self._claim_is_live(session):
            raise ReasoningRunInProgressError()
        took_over = session.status == RUNNING
        if took_over:
            logger.warning(
                "Reasoning session %s was left claimed by a run that did not finish; the lease "
                "has expired and it is being taken over.",
                session.id,
            )

        previous_status = session.status
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
        if result.rowcount == 0:
            # Ends the transaction rather than leaving it open on the way out. Nothing was
            # written — the UPDATE matched no row — but on PostgreSQL the row lock it took while
            # finding that out is held until the transaction ends, and the winner is mid-run.
            await self.db.commit()
            raise ReasoningRunInProgressError()

        # Inside the claim's transaction, after the swap has been won: an entry appended before
        # it would describe a run that the swap then refused, and one appended after the commit
        # would not be atomic with the claim it records. The audit append takes a
        # transaction-scoped advisory lock on PostgreSQL, so it is placed immediately before the
        # commit that releases it — the hold is this append rather than the whole panel.
        await self.audit.record(
            action="reasoning_run_started",
            # The account that made *this* request, not ``session.account_id``. The two are the
            # same while ``_session`` scopes a session to the account that opened it, and that
            # is exactly why it must not be read off the row: "who ran the panel" is a fact
            # about the caller, and sourcing it from the session would keep looking right on
            # the day sessions become shareable across a practice.
            account_id=account_id,
            patient_id=session.patient_id,
            entity_type="reasoning_session",
            entity_id=session.id,
            # Which clinician ran it is ``account_id``; when is ``created_at``. These two say
            # what the run started from, which is what distinguishes a first run from a re-run
            # after the chart changed, a retry after a failure, and a takeover of a run that
            # died. ``took_over_abandoned_run`` is derivable from ``previous_status`` only by
            # someone who knows the lease rule, and it is the fact a reviewer scans for: it
            # means an earlier run of this case was started and never came back.
            payload={
                "previous_status": previous_status,
                "took_over_abandoned_run": took_over,
            },
        )
        await self.db.commit()
        session.status = RUNNING
        session.run_claimed_at = claimed_at

    async def _release_claim_as_failed(
        self,
        account_id: uuid.UUID,
        session: ReasoningSession,
        patient_id: uuid.UUID,
        claimed_at: datetime | None,
        exc: BaseException,
    ) -> None:
        """Mark a claimed run failed and let go of the session.

        ``describe_exception`` rather than ``str(exc)``: exceptions on this path come back from
        an LLM provider that was just sent this patient's snapshot, and ``audit_logs.payload``
        is unencrypted, immutable and never pruned — so a provider that quoted the prompt in
        its error would write a piece of the chart into the one table that outlives the record.

        Takes ``patient_id`` and ``claimed_at`` alongside ``session`` rather than reading them
        off it, because the recovery below may have to roll back, and a rolled-back session
        expires every loaded object — reading ``session.patient_id`` afterwards would emit lazy
        IO from a context with no greenlet to run it in. The instance itself is only written
        back at the end, through a ``refresh`` that repopulates it either way.

        The rollback is attempted only if the release itself fails, and that ordering is the
        point. What this handles is no longer only "the panel raised": it now also covers the
        chart snapshot and the context build, and a *database* error there leaves the
        transaction unusable, so the UPDATE that releases the claim would fail on the poisoned
        connection and the claim would leak exactly as before. Rolling back unconditionally
        would fix that and expire the caller's other loaded objects on every ordinary LLM
        failure too, which is a wide blast radius for a narrow case — so it is the fallback,
        not the first move.

        The ``run_claimed_at`` predicate stops a dead run from stamping ``failed`` on a live
        one. A run that overruns ``reasoning_run_lease_minutes`` — retried LLM calls make that
        reachable, not hypothetical — can have had its session taken over by a second claim
        while it was still going. Without the predicate, its eventual failure would flip the
        *successor's* session to ``failed`` underneath a clinician watching that run stream,
        and the successor's own completion would then write over it. Matching nothing is the
        correct outcome there, and it is also why the audit entry is conditional: the run whose
        failure this is has nothing left to say about a session it no longer holds.
        """
        failure = describe_exception(exc)
        # Read before the expire below, which expires the primary key along with everything else.
        session_id = session.id
        # Drop the run's in-memory idea of this session before writing the row. ``_claim_for_run``
        # assigns ``status`` and ``run_claimed_at`` onto the instance after its own commit, which
        # leaves it *dirty* against the values it was loaded with — so the commit at the end of
        # this method would flush ``status='reasoning'`` straight back over the ``failed`` the
        # UPDATE below just wrote, and the session would come out of a failed run still claimed.
        # It only escaped notice because a run that got as far as ``_build_context`` had already
        # flushed that assignment on the way through, so the overwrite was invisible for exactly
        # the failures the old code could reach. From here the row is the truth and the instance
        # is reloaded from it.
        self.db.expire(session)
        release = (
            update(ReasoningSession)
            .where(
                ReasoningSession.id == session_id,
                ReasoningSession.run_claimed_at == claimed_at,
            )
            .values(status="failed", error_detail=failure)
            # As in ``_claim_for_run``: the ORM's post-update sync would re-evaluate this WHERE
            # clause in Python against a tz-aware claim and the naive datetime SQLite returns.
            .execution_options(synchronize_session=False)
        )
        try:
            result = cast("CursorResult[Any]", await self.db.execute(release))
        except SQLAlchemyError:
            await self.db.rollback()
            result = cast("CursorResult[Any]", await self.db.execute(release))

        if result.rowcount:
            await self.audit.record(
                action="reasoning_session_failed",
                account_id=account_id,
                patient_id=patient_id,
                entity_type="reasoning_session",
                entity_id=session_id,
                payload={"error": failure},
            )
        await self.db.commit()
        # The UPDATE went round the ORM, so without this the identity map still holds
        # ``reasoning`` — and with ``expire_on_commit=False`` a later ``get_session`` in the same
        # unit of work would hand that stale instance back rather than re-reading the row. A
        # refresh rather than two assignments because it also un-expires the instance if the
        # rollback above ran, and because it reports the row as it actually committed.
        await self.db.refresh(session)

    async def _finish_claimed_run(
        self,
        session: ReasoningSession,
        claimed_at: datetime | None,
        status: str,
        state: CaseState,
        output: dict,
    ) -> None:
        """Write the run's terminal header — but only if this run still holds the claim.

        The other half of a predicate this service already applies to its failure path.
        ``_release_claim_as_failed`` matches on ``run_claimed_at``, and says why: a run can
        outlast ``reasoning_run_lease_minutes``, have its session taken over while it is still
        going, and must not then stamp ``failed`` on the successor. That is not a remote timing.
        The comment sizing the lease reasons that a full panel's ceiling is
        ``llm_request_timeout_seconds`` per call; the real ceiling of one
        ``LLMClient.complete_json`` is that timeout times ``retries + 1`` times every provider in
        the fallback chain — 30s x 3 x 3 on the shipped defaults, for a *single* agent call, of
        which ``run_reasoning`` makes seven in sequence plus a parallel panel. A provider that
        hangs rather than refusing takes a run well past fifteen minutes.

        The success path — the one that writes the *immutable* rows — had no such predicate, and
        what stands in for it today is an accident. ``run`` flushes before the panel starts, and
        the session instance is dirty at that point (``_claim_for_run`` assigns ``status`` and
        ``run_claimed_at`` onto it after its own commit), so the flush issues an UPDATE on
        ``reasoning_sessions`` and the run holds that row's write lock for the whole panel.
        A takeover's compare-and-swap therefore blocks on PostgreSQL until this run commits and
        then matches nothing — and on SQLite fails outright with "database is locked". So the
        overrun is currently unreachable through two overlapping requests.

        It is unreachable by coincidence, not by design. Nothing in ``run`` intends to hold a row
        lock across the panel; the assignments that cause it are described at their own site as a
        workaround for the instance going stale, exactly the kind of line a later edit removes.
        Drop them, or commit anywhere inside the panel, and the lock goes with them — at which
        point an overrunning run writes a second complete set of ClinicalSuggestion rows against
        one ``session_id``, immutable by database trigger (Rule #7) and impossible to remove, over
        a header describing the run that took over. Two interleaved differentials under one case,
        with the header — if the runs disagreed — reading ``suggestive`` above the other run's
        hard block.

        So the claim is re-asserted explicitly, where the invariant can be read rather than
        inferred from lock ordering. It runs *before* anything is persisted: the enclosing
        transaction would roll the suggestions back anyway, but ordering the check first means a
        losing run never takes the insert path or the audit append's global lock at all. Raising
        leaves the transaction to the request teardown, and the successor's claim untouched.
        """
        if claimed_at is None:
            # Unreachable through ``run``: ``_claim_for_run`` always stamps a claim before this
            # is called. Guarded anyway because ``run_claimed_at == None`` renders as ``IS NULL``
            # in SQLAlchemy, so a None here would match a session with *no* claim and publish
            # over it — the one failure mode this method exists to make impossible.
            raise ReasoningRunSupersededError(detail="run finished holding no claim")

        result = cast(
            "CursorResult[Any]",
            await self.db.execute(
                update(ReasoningSession)
                .where(
                    ReasoningSession.id == session.id,
                    ReasoningSession.run_claimed_at == claimed_at,
                )
                .values(
                    status=status,
                    case_state=output["case_state"],
                    autonomy_tier=state.autonomy_tier,
                    completed_at=datetime.now(UTC),
                )
                # As in ``_claim_for_run`` and ``_release_claim_as_failed``: the ORM's post-update
                # synchronisation re-evaluates this WHERE clause in Python and raises comparing a
                # tz-aware claim against the naive datetime SQLite returns.
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount == 0:
            logger.warning(
                "Reasoning session %s was taken over by another run while this one was still "
                "executing; its output is being discarded rather than written over the "
                "successor's.",
                session.id,
            )
            raise ReasoningRunSupersededError()

        # The UPDATE went round the ORM, so the instance still reads as it did before it. Written
        # back rather than expired because the caller keeps using this instance — it is what
        # ``_persist_suggestions`` reads ``patient_id`` off, and what ``run`` returns.
        session.status = status
        session.case_state = output["case_state"]
        session.autonomy_tier = state.autonomy_tier

    # --------------------------------------------------------------- pipeline
    async def run(
        self, account_id: uuid.UUID, session_id: uuid.UUID, emit: EventEmitter | None = None
    ) -> tuple[ReasoningSession, list[ClinicalSuggestion]]:
        session = await self._session(account_id, session_id)
        # Exclusive from here: nothing else may run this session until the claim is released or
        # its lease expires. Taken before the snapshot is assembled so a losing caller spends one
        # UPDATE rather than a chart read.
        await self._claim_for_run(account_id, session)
        # Read off the instance before the try. The handler expires it — and may roll back — so
        # neither can be asked for again from inside it. See ``_release_claim_as_failed``.
        patient_id = session.patient_id
        claimed_at = session.run_claimed_at
        try:
            # Inside the failure handler, not before it. These two steps used to sit between the
            # committed claim and the ``try`` — and they are the two heaviest things the method
            # does: ``_rebuild_intake_state`` assembles the whole chart snapshot, and
            # ``_build_context`` re-evaluates every current medication for safety and loads the
            # guideline corpus. A failure in either escaped the handler entirely, so the session
            # kept the claim and sat at ``reasoning`` until the lease expired: unrunnable by
            # anyone for fifteen minutes, with no ``failed`` status and no closing audit record
            # — indistinguishable in the trail from a run that reached the panel and was
            # abandoned mid-flight, which is the one thing that trail exists to distinguish.
            #
            # When the LLM is unavailable the pipeline still runs deterministically (degraded
            # mode): it marks the case degraded, escalates to flag-for-review, and attaches an
            # explicit caveat rather than silently producing confident output.
            state = await self._rebuild_intake_state(session)
            state.intake_complete = True
            await self.db.flush()
            ctx = await self._build_context(account_id, patient_id, emit=emit)
            output = await graph.run_reasoning(state, ctx)
            # Inside the try, because a re-check that cannot be completed must fail the run
            # rather than let it publish output nothing confirmed was still safe.
            output, appeared = await self._recheck_chart_safety(
                account_id, patient_id, state, output, emit=emit
            )
        except Exception as exc:  # noqa: BLE001 — record failure, surface to clinician
            try:
                await self._release_claim_as_failed(
                    account_id, session, patient_id, claimed_at, exc
                )
            except Exception:  # noqa: BLE001 — never mask the failure being reported
                # The release is best-effort by construction: it is itself database work, and
                # the failure it is reporting may be the database being unreachable. Losing it
                # costs the lease — the session stays claimed until
                # ``reasoning_run_lease_minutes`` elapses and the next run takes it over — which
                # is the safety net this exists to avoid needing, not one it may destroy. What
                # must not happen is this swallowing or replacing the exception the clinician
                # is waiting on an answer about.
                logger.exception(
                    "Could not release the run claim on reasoning session %s after a failed "
                    "run; it will stay claimed until the lease expires.",
                    session_id,
                )
            raise

        # Before a single row is written. The claim taken above is only exclusive for the length
        # of its lease, and this run may have outlasted it — see ``_finish_claimed_run``.
        status = "awaiting_review" if state.autonomy_tier == "flag_for_review" else "completed"
        await self._finish_claimed_run(session, claimed_at, status, state, output)
        suggestions = await self._persist_suggestions(account_id, session, state, output)

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
        if appeared:
            # Its own entry, separate from ``hard_block_triggered``, because the two answer
            # different questions. That one says the run ended with hard blocks; this says the
            # chart moved under a run in flight and the panel reasoned without part of it —
            # which is what a reviewer asking "why does this run's differential not mention the
            # allergy it was blocked on?" needs, and what nothing else in the trail records.
            closing.append(
                AuditDraft(
                    action="reasoning_chart_changed_under_run",
                    account_id=account_id,
                    patient_id=session.patient_id,
                    entity_type="reasoning_session",
                    entity_id=session.id,
                    payload={"hard_blocks_appeared": len(appeared)},
                )
            )
        await self.audit.record_many(closing)
        await self.db.commit()
        return session, suggestions

    async def _recheck_chart_safety(
        self,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        state: CaseState,
        output: dict[str, Any],
        *,
        emit: EventEmitter | None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Re-evaluate the chart's own drug safety against the record as it stands *now*.

        Returns the output to publish and the summaries of any hard block the panel did not see.

        The panel deliberates over a chart frozen when the run's context was built, and a run is
        minutes of LLM calls long. The current-medication arm of ``drug_safety_check`` was frozen
        with it — ``_safety_evaluator`` precomputes that arm — so anything documented while the
        panel was thinking was invisible to it. One clinician does not have to be careless for
        that window to matter: the arrangement this product is built for is a practice login used
        from two rooms, and "start the reasoning, then go and enter what the patient just told
        you about their reactions" is the ordinary way to work, not a race someone has to
        contrive. Approving an extracted prescription mid-run does it just as well.

        What that cost, before this: a patient on aspirin, an aspirin allergy documented while
        the panel ran, and a completed run reporting **no hard block at all** — the same chart
        evaluated from the start raises one. Critical Safety Rule #3 says an allergy conflict is
        a hard block rather than a warning, and it was being decided by which of two things
        happened first. The clinician is shown a finished run over the current chart with nothing
        marking it stale, which is worse than a slow answer and worse than no answer.

        So the deterministic evaluation is redone here, at the last moment before anything is
        written, and it is the evaluation that ships with the run. Cheap enough to be
        unconditional: a handful of indexed queries against a run that just spent minutes on the
        panel, and no LLM — so it works identically in degraded mode, which is the point of Rule
        #8. Only the *chart's own* arm is redone; the management-option arm already screened live
        (``SafetyService.screen_text`` builds its context per call), which is how one run could
        hard-block a guideline suggestion naming a drug while saying nothing about the same
        conflict against the patient's own medication list.

        A hard block that appeared is added and forces flag-for-review — never removed, and
        neither is one the panel raised that has since gone away. Conservative wins (Rule #2):
        the fresh read decides what to *add*, not what to withdraw, so a retracted allergy leaves
        a block the clinician can override with documented reasoning rather than one that
        silently vanished. The blocks reach the clinician through the ordinary path — synthesis
        rebuilds the suggestion list from the corrected state — so they carry the same shape,
        the same override requirement and the same audit as any other.

        This runs after the Verifier and does not go through it, which Rule #1 otherwise forbids.
        It is not an exception to that rule: nothing here is generated. A deterministic table
        lookup can only add a hard block and escalate the tier to flag-for-review, which is the
        most conservative verdict the Verifier itself can reach — so this can only move the
        output in the direction the Verifier is there to move it, never past it.
        """
        service = SafetyService(self.db)
        results = await service.active_flags(account_id=account_id, patient_id=patient_id)
        fresh = [_flag_dict(f) for _vocab, flag_list in results for f in flag_list]
        # The same chart-level flags the pre-panel arm carried (see ``_safety_evaluator``), and
        # re-read here for the same reason everything else in this method is: a medication
        # approved onto the chart mid-run can be one the vocabulary cannot resolve, and this is
        # the evaluation that ships.
        fresh += [_flag_dict(f) for f in await service.chart_completeness_flags(patient_id)]
        known = {block.summary for block in state.hard_blocks}
        state.drug_safety_flags = fresh
        for flag in fresh:
            record_hard_block(state, flag)
        appeared = [b.summary for b in state.hard_blocks if b.summary not in known]

        if appeared:
            state.autonomy_tier = more_conservative_tier(state.autonomy_tier, "flag_for_review")
            state.add_trace(
                "drug_safety_recheck",
                f"{len(appeared)} hard block(s) documented on the chart while this run was in "
                "flight; the panel did not see them",
                {},
            )
        if emit is not None:
            # Last-wins in the Theatre's reducer, so this replaces the pre-panel picture rather
            # than appearing beside it. Emitted whether or not a block appeared: the flag list
            # shown live has to be the one that shipped, and "the re-check agreed" is not
            # something the clinician can infer from an event that never arrives.
            await emit(
                "drug_safety",
                {
                    "agent": "drug_safety_check",
                    "flags": fresh,
                    "management_flags": [
                        {"option": o.text, "flags": o.safety_flags}
                        for o in state.management_options
                        if o.safety_flags
                    ],
                    "hard_blocks": len(state.hard_blocks),
                    "appeared_during_run": len(appeared),
                },
            )
        # Rebuilt rather than patched, so the published output cannot drift from the state it is
        # supposed to describe. ``build_suggestions`` is a pure function of the state and the
        # re-check is normally a no-op, in which case this reproduces what synthesis already
        # returned.
        return {
            "suggestions": synthesis.build_suggestions(state),
            "case_state": state.to_dict(),
        }, appeared

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
            task.cancel()
            # Cancelled *and waited for*. Cancelling only schedules the CancelledError: the
            # worker is left in the "cancelling" state and does not unwind until it next reaches
            # the event loop, which is after this generator has returned. And returning is what
            # ends the request — ``get_db`` closes the session on the way out — so the worker was
            # being handed a ``CancelledError`` at some await inside ``run`` at the same moment
            # the ``AsyncSession`` it is using was being torn down under it.
            #
            # That is not a theoretical overlap. Closing the Reasoning Theatre tab is the ordinary
            # way a run ends (see ``ReasoningSession.run_in_progress``), and the worker spends
            # almost all of its life inside ``self.db`` — the snapshot, the safety evaluation, the
            # suggestion inserts, the audit appends, the final commit. An ``AsyncSession`` is not
            # safe for concurrent use, and on asyncpg two operations on one connection is an
            # ``InterfaceError``; a connection interrupted mid-statement and then closed goes back
            # to the pool in a state the next request inherits. Waiting costs one scheduler turn
            # in the common case and makes "the request is over" mean the worker is done.
            #
            # Only ``CancelledError`` is suppressed, and only the one this cancel just caused:
            # ``worker`` catches ``Exception`` itself and relays it to the stream, so nothing else
            # comes out of the await. If this generator is being closed *because* the enclosing
            # request task was cancelled, that cancellation is still pending on the outer task and
            # resumes at its next await — suppressing here ends the worker, not the outer unwind.
            with contextlib.suppress(asyncio.CancelledError):
                await task

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
