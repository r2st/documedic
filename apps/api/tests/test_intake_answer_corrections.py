"""What happens when the same intake question is answered more than once.

The intake interview is clinical *input*, not output: ``text_blob`` feeds affirmative answers to
the can't-miss sentinel and deliberately drops the keywords of anything answered "no", so a
``red_flag`` answer decides whether a time-critical diagnosis is screened in or out. Which answer
the engine reads therefore has to be the one the clinician last gave, every time.

Three ways the same question gets answered twice, none of them exotic:

  * the clinician corrects a mistake before running the engine;
  * the client retries a submission whose response was lost, or the user double-submits;
  * one payload carries the same ``question_id`` twice.

``SubmitAnswersRequest`` allows all three -- it validates a list of up to 50 answers with no
uniqueness constraint on ``question_id`` -- and none of them is a client bug the server may
assume away.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models.intake import IntakeAnswer
from app.models.user import Account
from app.services.reasoning_service import ReasoningService
from tests.conftest import create_patient


async def _account_id(db) -> uuid.UUID:
    """The id of the account the ``auth_client`` fixture signed up."""
    account_id = (await db.execute(select(Account.id))).scalars().first()
    assert account_id is not None, "no account exists — use the auth_client fixture"
    return account_id


async def _answer_rows(db, session_id, question_id) -> list[str]:
    result = await db.execute(
        select(IntakeAnswer.answer_text).where(
            IntakeAnswer.session_id == session_id, IntakeAnswer.question_id == question_id
        )
    )
    return list(result.scalars().all())


async def _start(db, auth_client, complaint="fever for three days"):
    patient = await create_patient(auth_client)
    account_id = await _account_id(db)
    service = ReasoningService(db)
    session, questions = await service.start(account_id, uuid.UUID(patient["id"]), complaint)
    assert questions, "the triage agent generated no questions to answer"
    return service, account_id, session, questions


async def _keep_intake_open(db, session) -> None:
    """Hold the interview open across submissions.

    A correction only exists while intake is still gathering: once ``intake_complete`` is set,
    ``submit_answers`` deliberately no-ops (pinned by
    ``test_submitting_answers_after_intake_is_complete_is_a_no_op``) so a late answer cannot
    reopen a closed interview. Under the simulated triage agent one round is enough to close it,
    where a real multi-round interview stays open across several submissions — which is where a
    clinician revises an earlier answer.
    """
    session.intake_complete = False
    await db.flush()


async def test_correcting_an_answer_is_what_the_engine_reads(db, auth_client):
    """The clinical case: a red-flag answer given wrongly, then corrected.

    Before this, a correction inserted a second ``intake_answers`` row and
    ``_rebuild_intake_state`` collapsed the rows with ``answers_by_q[a.question_id] = ...`` over
    an *unordered* query -- so which answer reached the engine was whatever order the database
    happened to return, and the correction could be silently discarded. Answering "yes" to "any
    chest pain?" and then correcting it to "no" is precisely the input that decides whether the
    can't-miss sentinel screens acute coronary syndrome in or out.
    """
    service, account_id, session, questions = await _start(db, auth_client)
    question_id = questions[0].id

    await service.submit_answers(
        account_id, session.id, [{"question_id": question_id, "answer_text": "yes"}]
    )
    await _keep_intake_open(db, session)
    await service.submit_answers(
        account_id, session.id, [{"question_id": question_id, "answer_text": "no"}]
    )

    state = await service._rebuild_intake_state(session)
    answered = {q.text: q.answer for q in state.intake_questions}

    assert answered[questions[0].question_text] == "no", "the correction was discarded"
    # The value above is only *guaranteed* because one row is stored. With two rows the read in
    # ``_rebuild_intake_state`` is unordered, and SQLite happens to hand them back in insertion
    # order — so this assertion alone passes on the test database while staying undefined on the
    # PostgreSQL the application actually runs on. What makes the answer deterministic is that
    # there is nothing to order.
    assert await _answer_rows(db, session.id, question_id) == ["no"]


async def test_re_answering_does_not_accumulate_rows(db, auth_client):
    """One question, one answer of record — so there is no ordering left to get wrong.

    Resolving duplicates by "latest wins" would need a tiebreak the schema cannot supply: two
    rows written in the same transaction share a ``created_at``, and the primary key is a random
    UUID rather than a sequence. Keeping exactly one row makes the clinical input deterministic
    by construction instead of by sort order.
    """
    service, account_id, session, questions = await _start(db, auth_client)
    question_id = questions[0].id

    for text in ("yes", "no", "unsure"):
        await _keep_intake_open(db, session)
        await service.submit_answers(
            account_id, session.id, [{"question_id": question_id, "answer_text": text}]
        )

    assert await _answer_rows(db, session.id, question_id) == ["unsure"]


async def test_a_retried_submission_is_idempotent(db, auth_client):
    """A lost response or a double-clicked button must not double-record the interview."""
    service, account_id, session, questions = await _start(db, auth_client)
    payload = [{"question_id": q.id, "answer_text": "no"} for q in questions]

    await service.submit_answers(account_id, session.id, payload)
    before = await db.scalar(
        select(func.count()).select_from(IntakeAnswer).where(IntakeAnswer.session_id == session.id)
    )
    # Held open deliberately: with the interview already closed the second call would no-op on
    # the ``intake_complete`` guard and this would pass without exercising the write path at all.
    await _keep_intake_open(db, session)
    await service.submit_answers(account_id, session.id, payload)
    after = await db.scalar(
        select(func.count()).select_from(IntakeAnswer).where(IntakeAnswer.session_id == session.id)
    )

    assert after == before, f"a retried submission grew the interview from {before} to {after}"


async def test_the_same_question_twice_in_one_payload_takes_the_last_answer(db, auth_client):
    """One payload, two answers for one question.

    Nothing rejects this — ``SubmitAnswersRequest`` validates a list, not a set — and processing
    both wrote two rows in a single transaction, the one case where a ``created_at`` tiebreak
    could never have resolved them. Last wins, matching what re-submitting would do.
    """
    service, account_id, session, questions = await _start(db, auth_client)
    question_id = questions[0].id

    await service.submit_answers(
        account_id,
        session.id,
        [
            {"question_id": question_id, "answer_text": "yes"},
            {"question_id": question_id, "answer_text": "no"},
        ],
    )

    assert await _answer_rows(db, session.id, question_id) == ["no"]


async def test_answers_to_other_questions_are_untouched_by_a_correction(db, auth_client):
    """A correction must rewrite its own question's answer and nothing else."""
    service, account_id, session, questions = await _start(db, auth_client)
    if len(questions) < 2:
        import pytest

        pytest.skip("triage generated a single question; nothing to hold constant")

    await service.submit_answers(
        account_id,
        session.id,
        [{"question_id": q.id, "answer_text": f"answer-{i}"} for i, q in enumerate(questions)],
    )
    await _keep_intake_open(db, session)
    await service.submit_answers(
        account_id, session.id, [{"question_id": questions[0].id, "answer_text": "corrected"}]
    )

    state = await service._rebuild_intake_state(session)
    by_text = {q.text: q.answer for q in state.intake_questions}

    assert by_text[questions[0].question_text] == "corrected"
    for i, q in enumerate(questions[1:], start=1):
        assert by_text[q.question_text] == f"answer-{i}", "an unrelated answer changed"
    # One row per question, for the same reason as above: the values are only deterministic
    # because nothing is left for the database to choose between.
    for q in questions:
        assert len(await _answer_rows(db, session.id, q.id)) == 1
