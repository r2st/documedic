"""One answer of record per clarifying question — including when two submissions overlap.

``ReasoningService.submit_answers`` has always treated a second answer to a question as a
correction that rewrites the first, and the reason it must is written out at the call site:
``_rebuild_intake_state`` collapses answers with ``answers_by_q[a.question_id] = a.answer_text``
over an **unordered** query, so two rows for one question mean the one that reaches the engine is
whatever order the database returned. That is clinical input, not bookkeeping —
``agents.util.text_blob`` feeds affirmative answers to the can't-miss sentinel and drops the
keywords of anything answered "no", so on a ``red_flag`` question it is what decides whether a
time-critical diagnosis is screened in or out. And the two rows cannot be told apart afterwards:
rows written in one transaction share a ``created_at`` and the primary key is a random UUID
rather than a sequence, so "the latest answer" is not a question the schema can answer.

The rule was enforced only in Python, by reading the stored answers and updating the row it
found. Read-then-insert — so two submissions overlapping inside that window both read no answer
and both inserted, and the invariant the engine depends on was the first casualty of the
arrangement this product is built for: one practice login, two rooms, a double-clicked Submit, a
retried request. A "yes" and a "no" to one red-flag question sat in the table together and the
sentinel got a coin toss.

``uq_intake_answers_question`` is the durable half (migration 0024). The tests below are split
between the ones that need genuine concurrency — a file-backed database with a real pool, for the
reasons ``test_concurrent_ingestion`` sets out — and the schema assertions that do not.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.seed import seed_all
from app.exceptions import ConcurrentAnswerError
from app.models import Base
from app.models.intake import IntakeAnswer, IntakeQuestion
from app.models.patient import Patient
from app.models.user import Account
from app.services.reasoning_service import ReasoningService


@pytest_asyncio.fixture
async def file_sessionmaker(tmp_path: Path):
    """A file-backed engine, so two sessions genuinely contend rather than share a connection."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'intake.db'}", connect_args={"timeout": 30}
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.exec_driver_sql("PRAGMA busy_timeout=30000")
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as db:
        await seed_all(db)
    yield maker
    await engine.dispose()


async def _case(maker) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """An account, an open reasoning session, and the id of its first pending question."""
    async with maker() as db:
        account = Account(email=f"intake-{uuid.uuid4().hex}@example.com", password_hash="x")
        db.add(account)
        await db.flush()
        patient = Patient(
            account_id=account.id,
            full_name="Intake Race",
            sex="male",
            date_of_birth=datetime(1957, 9, 4).date(),
            consent_given=True,
            consent_given_at=datetime.now(UTC),
        )
        db.add(patient)
        await db.commit()
        account_id, patient_id = account.id, patient.id

    async with maker() as db:
        session, questions = await ReasoningService(db).start(
            account_id, patient_id, "crushing chest pain since 6am, radiating to the left arm"
        )
        return account_id, session.id, questions[0].id


async def _answers_to(maker, question_id: uuid.UUID) -> list[str]:
    async with maker() as db:
        return list(
            (
                await db.execute(
                    select(IntakeAnswer.answer_text).where(IntakeAnswer.question_id == question_id)
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_two_simultaneous_answers_leave_one_answer_of_record(file_sessionmaker):
    """The race itself. Two submissions, one question, and the chart may hold only one answer.

    Both read the stored answers before either has written, so both decide to insert. What
    ``uq_intake_answers_question`` guarantees is that the second insert is refused; what
    ``submit_answers`` then does is re-read and apply its answer over the winner's row. Either
    clinician's answer may end up being the one of record — that is genuinely undetermined and
    always was — but there must be exactly one, and it must be one of the two that were sent.
    """
    account_id, session_id, question_id = await _case(file_sessionmaker)

    async def submit(text: str) -> None:
        async with file_sessionmaker() as db:
            await ReasoningService(db).submit_answers(
                account_id, session_id, [{"question_id": question_id, "answer_text": text}]
            )

    await asyncio.gather(submit("yes"), submit("no"), return_exceptions=True)

    recorded = await _answers_to(file_sessionmaker, question_id)
    assert len(recorded) == 1, (
        f"{len(recorded)} answers of record for one question: {sorted(recorded)}. The reasoning "
        "engine reads whichever one the query returns first, so on a red-flag question this is a "
        "coin toss over whether a can't-miss diagnosis gets screened."
    )
    assert recorded[0] in ("yes", "no")


def _loses_n_times(service: ReasoningService, losses: int):
    """Make ``_record_answers`` lose the insert race ``losses`` times, then behave normally.

    The refusal is what a concurrent submission's committed row produces, and it is injected
    rather than raced because SQLite serialises writers with a database-wide lock: a test that
    genuinely held one transaction open inside the other's window would be measuring "database is
    locked", not the retry. The retry is a property of ``submit_answers``, and this asserts it
    directly. That the window itself is reachable is asserted by the ``gather`` tests above,
    which need no help to reach it.
    """
    real = service._record_answers  # noqa: SLF001 — the retry path is what is under test
    attempts = {"n": 0}

    async def flaky(session_id: uuid.UUID, answers: list[dict]) -> None:
        attempts["n"] += 1
        if attempts["n"] <= losses:
            raise IntegrityError("INSERT INTO intake_answers", {}, Exception("unique constraint"))
        await real(session_id, answers)

    return flaky, attempts


@pytest.mark.asyncio
async def test_a_submission_that_lost_the_race_still_lands(file_sessionmaker, monkeypatch):
    """Losing the insert must not mean losing the answer, while intake is still open.

    Catching the refusal and re-reading is the whole point of not letting it out: the clinician
    answered a question, and a submission that loses a race has done nothing wrong. The retry puts
    it back on the sequential path — read what is stored, apply this answer over it — which is
    where it would have been had the two requests not overlapped.

    (Once intake has closed, a submission is a no-op that returns the session unchanged, which is
    what a sequential second submission has always got. The retry does not reopen it.)
    """
    account_id, session_id, question_id = await _case(file_sessionmaker)

    async with file_sessionmaker() as db:
        service = ReasoningService(db)
        flaky, attempts = _loses_n_times(service, losses=1)
        monkeypatch.setattr(service, "_record_answers", flaky)
        await service.submit_answers(
            account_id, session_id, [{"question_id": question_id, "answer_text": "no"}]
        )

    assert attempts["n"] == 2, "the losing submission was not retried"
    assert await _answers_to(file_sessionmaker, question_id) == ["no"], (
        "the clinician's answer was dropped rather than re-applied after the refusal"
    )


@pytest.mark.asyncio
async def test_the_same_question_twice_in_one_payload_is_still_one_answer(file_sessionmaker):
    """``SubmitAnswersRequest`` validates a list, not a set. Last one in the payload wins."""
    account_id, session_id, question_id = await _case(file_sessionmaker)

    async with file_sessionmaker() as db:
        await ReasoningService(db).submit_answers(
            account_id,
            session_id,
            [
                {"question_id": question_id, "answer_text": "yes"},
                {"question_id": question_id, "answer_text": "no"},
            ],
        )

    assert await _answers_to(file_sessionmaker, question_id) == ["no"]


@pytest.mark.asyncio
async def test_resubmitting_the_same_answer_is_idempotent(file_sessionmaker):
    account_id, session_id, question_id = await _case(file_sessionmaker)

    for _ in range(3):
        async with file_sessionmaker() as db:
            await ReasoningService(db).submit_answers(
                account_id, session_id, [{"question_id": question_id, "answer_text": "yes"}]
            )

    assert await _answers_to(file_sessionmaker, question_id) == ["yes"]


@pytest.mark.asyncio
async def test_a_submission_that_keeps_losing_is_a_conflict_not_a_server_error(
    file_sessionmaker, monkeypatch
):
    """When even the retry loses, the clinician gets a 409 rather than a 500 or a duplicate.

    A third submission arriving inside the retry's own window is remote, but the alternative to
    handling it is either an unhandled ``IntegrityError`` — a server fault for something the
    clinician did nothing wrong to cause — or letting the duplicate through, which is the state
    this whole file exists to make unreachable.
    """
    account_id, session_id, question_id = await _case(file_sessionmaker)

    async with file_sessionmaker() as db:
        service = ReasoningService(db)
        flaky, attempts = _loses_n_times(service, losses=99)
        monkeypatch.setattr(service, "_record_answers", flaky)

        with pytest.raises(ConcurrentAnswerError) as caught:
            await service.submit_answers(
                account_id, session_id, [{"question_id": question_id, "answer_text": "no"}]
            )

    assert caught.value.status_code == 409
    assert caught.value.code == "concurrent_answer"
    assert attempts["n"] == 2, (
        f"the retry loop is unbounded or does not retry at all: {attempts['n']} attempts"
    )
    # And nothing was written: refusing is the point, so a duplicate must not have slipped past.
    assert await _answers_to(file_sessionmaker, question_id) == []


def test_the_model_carries_the_unique_index() -> None:
    """Pinned on the model, because the Python check alone is what the race defeated."""
    indexes = {index.name: index for index in IntakeAnswer.__table__.indexes}
    unique = indexes.get("uq_intake_answers_question")
    assert unique is not None, "intake_answers has no uq_intake_answers_question index"
    assert unique.unique is True
    assert [column.name for column in unique.expressions] == ["question_id"]
    # The plain index the column used to carry is subsumed by the unique one and would only cost
    # a write on every answer recorded. See migration 0024.
    assert "ix_intake_answers_question_id" not in indexes


def test_migration_0024_creates_the_index_it_claims_to() -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "data"
        / "migrations"
        / "versions"
        / "0024_one_answer_per_intake_question.py"
    ).read_text()
    assert "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} " in source
    assert '_INDEX = "uq_intake_answers_question"' in source
    assert "ON intake_answers (question_id)" in source
    # The index cannot be built over data that already violates it.
    assert "DELETE FROM intake_answers" in source
    assert "ROW_NUMBER() OVER (" in source


@pytest.mark.asyncio
async def test_the_engine_reads_the_one_answer_that_survived(file_sessionmaker):
    """End of the chain: the deduplicated answer is what the case state is rebuilt from.

    A red-flag question answered "no" must reach the engine as "no". The invariant is only worth
    enforcing because of this step — ``text_blob`` drops the keywords of a negative answer, so
    which row survives is which way the can't-miss sentinel is pointed.
    """
    account_id, session_id, question_id = await _case(file_sessionmaker)

    async def submit(text: str) -> None:
        async with file_sessionmaker() as db:
            await ReasoningService(db).submit_answers(
                account_id, session_id, [{"question_id": question_id, "answer_text": text}]
            )

    await asyncio.gather(submit("yes"), submit("no"), return_exceptions=True)

    async with file_sessionmaker() as db:
        service = ReasoningService(db)
        session = await service.get_session(account_id, session_id)
        state = await service._rebuild_intake_state(session)  # noqa: SLF001 — what the engine sees
        question = (await db.get(IntakeQuestion, question_id)).question_text
        answers = [q.answer for q in state.intake_questions if q.text == question]

        stored = await db.scalar(
            select(func.count())
            .select_from(IntakeAnswer)
            .where(IntakeAnswer.question_id == question_id)
        )

    assert stored == 1
    assert answers and answers[0] in ("yes", "no")
