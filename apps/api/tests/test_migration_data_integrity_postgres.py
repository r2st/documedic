"""What happens to rows that are already in the table when a migration runs over them.

The gap this fills
------------------
Migration testing in this suite had two halves and no third. ``tests/test_migrations.py`` reads
the scripts as source and never runs them. ``tests/test_migration_chain_postgres.py`` runs the
whole chain, but against a database that is *empty* — so every reconciliation step in it matched
nothing, and a passing run proved only that the DDL parses and applies.

Four revisions in this chain do not just change the schema; they change data, and they change it
because the schema they are about to install would otherwise refuse the rows already there:

===========  ================================================================================
0010         backfills ``lab_results.dedup_key``, then soft-deletes duplicate observations so a
             unique index on (patient_id, dedup_key) can be created
0022         flips ``is_current`` off on medication *stop* events, which were never current
0024         hard-deletes duplicate ``intake_answers`` so one answer per question can be enforced
0026         soft-deletes documents duplicated within one chart so the per-chart hash index can
             be created
===========  ================================================================================

Each of those runs against a live clinical record. Getting them wrong is not a failed deploy —
a failed deploy is the *good* outcome here, because it is visible. Getting them wrong quietly
means an observation disappearing from a chart, or the wrong one of two rows surviving, on a
database where nobody is looking at the rows the upgrade touched. Nothing tested that.

So these tests seed the state each revision was written to reconcile, run the real
``alembic upgrade`` over it, and read the rows back:

* the upgrade completes (without the reconciliation, adding the index aborts it),
* the survivor is the *earliest* row, which is the rule each migration documents,
* and — the part that matters clinically — the losing row is still in the table.

Seeding happens at revision 0009 through the ORM. That works because revision 0001 builds the
schema from live ORM metadata, so at 0009 the tables already carry every column the models
declare — and it is also why the fixture has to drop three indexes before it can seed anything
at all: 0001 installs those too. See ``seeded_database``, which is the interesting part of this
file. It is the same property that made 0010 abort on a fresh database once (the chain test's
module docstring tells that story).

Runs only when a PostgreSQL is reachable — ``docker compose up -d postgres``, or point
``TEST_POSTGRES_URL`` elsewhere. Its scratch database is its own, created and dropped per test.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.document import Document
from app.models.intake import IntakeAnswer, IntakeQuestion
from app.models.lab_result import LabResult, lab_observation_key
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.reasoning_session import ReasoningSession
from app.models.user import Account

# The chain test owns the plumbing for driving alembic against a scratch database; this module
# is the second caller rather than a second copy of it.
from tests.postgres_required import unavailable
from tests.test_migration_chain_postgres import (
    DEFAULT_URL,
    _admin_execute,
    _alembic,
    _swap_database,
)

pytestmark = pytest.mark.postgres

SCRATCH_DATABASE = "documedic_migration_data_test"

# The revision to seed at: the last one before 0010, which is the first of the four that
# reconciles existing rows.
BEFORE_RECONCILIATION = "0009"

EARLIER = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
LATER = EARLIER + timedelta(hours=6)

# The instant a specimen was drawn. A timezone-aware ``datetime``, because that is what the only
# writer of this column produces: ``GraphService._parse_datetime`` returns tz-aware or None, and
# ``lab_observation_key`` renders it to a UTC ISO string. A bare ``date`` here would key
# differently from the value PostgreSQL hands back for the same row — see
# ``test_the_dedup_key_survives_a_round_trip_through_the_database``, which is the reason that
# matters to 0010 specifically.
SAMPLED = datetime(2026, 2, 20, 7, 30, tzinfo=UTC)

# The unique indexes 0010, 0024 and 0026 exist to install — dropped by the fixture below to get
# back to the state each revision was written against. See its docstring.
INDEXES_THE_UPGRADE_INSTALLS = (
    "uq_lab_results_observation",
    "uq_intake_answers_question",
    "uq_documents_patient_hash",
)


@pytest_asyncio.fixture
async def seeded_database():
    """A scratch database in the state a pre-0010 production database was in.

    Yields ``(database_url, sessionmaker)``. The caller seeds through the sessionmaker, then
    runs ``_alembic(database_url, "upgrade", "head")`` itself — the upgrade is the thing under
    test, so it does not belong in the fixture.

    Getting to that state takes one step beyond `upgrade 0009`, and the reason is the same
    property that made 0010 abort on a fresh database (see the chain test's docstring): revision
    0001 builds the schema from *live* ORM metadata, and the models declare these unique
    indexes. So a database freshly upgraded to 0009 already has the constraints that 0010, 0024
    and 0026 were written to add — it will not accept a duplicate row, and their reconciliation
    steps would match nothing however carefully they were written.

    That is worth being precise about, because it is a claim about which databases those
    revisions can still act on. On a database created today they are no-ops. The databases they
    are *for* are the ones created before the index existed — a deployment that has been running
    since 0009 and is upgrading now — and that database is reproduced here by dropping the
    indexes, which is not a trick to make the test work but is literally the schema that
    deployment has.
    """
    url = os.environ.get("TEST_POSTGRES_URL", DEFAULT_URL)
    try:
        await _admin_execute(url, f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}"')
        await _admin_execute(url, f'CREATE DATABASE "{SCRATCH_DATABASE}"')
    except Exception as exc:  # noqa: BLE001 — unreachable, or no privilege to create one
        unavailable(f"no PostgreSQL to build a scratch database on ({type(exc).__name__}): {exc}")

    database_url = _swap_database(url, SCRATCH_DATABASE)
    try:
        result = _alembic(database_url, "upgrade", BEFORE_RECONCILIATION)
        assert result.returncode == 0, (
            f"could not reach {BEFORE_RECONCILIATION} to seed from:\n{result.stderr}"
        )
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                for index in INDEXES_THE_UPGRADE_INSTALLS:
                    await conn.execute(text(f'DROP INDEX IF EXISTS "{index}"'))
            yield database_url, async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
    finally:
        await _admin_execute(url, f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}"')


async def _chart(db: AsyncSession) -> Patient:
    account = Account(email=f"mig-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Migration Test Patient",
        sex="female",
        date_of_birth=date(1971, 4, 2),
        consent_given=True,
        consent_given_at=EARLIER,
    )
    db.add(patient)
    await db.flush()
    return patient


def _upgraded(database_url: str) -> None:
    result = _alembic(database_url, "upgrade", "head")
    assert result.returncode == 0, (
        "the upgrade aborted over data that was already in the table — which is what the "
        f"reconciliation step in the revision exists to prevent:\n{result.stderr}"
    )


# --- 0010: two readings of the same observation ------------------------------------------------
#
# The realistic source is one lab report uploaded twice, or an extraction re-run over the same
# document: identical marker, value and sample date, so identical dedup_key. Both rows are live,
# and the unique index 0010 installs cannot be created while they are.


@pytest_asyncio.fixture
async def duplicate_observations(seeded_database):
    database_url, sessionmaker = seeded_database
    async with sessionmaker() as db:
        patient = await _chart(db)
        shared = {
            "patient_id": patient.id,
            "marker_name": "Creatinine",
            "value_numeric": Decimal("1.4"),
            "unit": "mg/dL",
            "sample_date": SAMPLED,
        }
        first = LabResult(**shared, created_at=EARLIER)
        second = LabResult(**shared, created_at=LATER)
        db.add_all([first, second])
        await db.commit()
        assert first.dedup_key == second.dedup_key, (
            "the fixture did not actually build a duplicate; the rest of this test would be "
            "asserting nothing"
        )
        return database_url, sessionmaker, patient.id, first.id, second.id


@pytest.mark.asyncio
async def test_a_duplicated_observation_does_not_stop_the_upgrade(duplicate_observations):
    database_url, *_ = duplicate_observations
    _upgraded(database_url)


@pytest.mark.asyncio
async def test_the_earlier_of_two_duplicate_observations_is_the_one_left_live(
    duplicate_observations,
):
    """Earliest by (created_at, id) — "recorded first", with the key breaking ties."""
    database_url, sessionmaker, patient_id, first_id, second_id = duplicate_observations
    _upgraded(database_url)

    async with sessionmaker() as db:
        live = (
            (
                await db.execute(
                    select(LabResult.id).where(
                        LabResult.patient_id == patient_id,
                        LabResult.is_deleted.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert list(live) == [first_id], f"expected only the earlier row live, got {live}"


@pytest.mark.asyncio
async def test_the_duplicate_observation_is_retired_and_not_destroyed(duplicate_observations):
    """A soft delete, not a DELETE. The row is a clinical observation that was really recorded,
    and an upgrade that quietly removes one is worse than an upgrade that fails."""
    database_url, sessionmaker, patient_id, _first_id, second_id = duplicate_observations
    _upgraded(database_url)

    async with sessionmaker() as db:
        row = await db.get(LabResult, second_id)
    assert row is not None, "the losing duplicate was deleted outright"
    assert row.is_deleted is True
    assert row.value_numeric == Decimal("1.4"), "the retired row's clinical content was altered"


@pytest.mark.asyncio
async def test_two_observations_that_only_look_alike_both_survive(seeded_database):
    """The other direction, and the one that would be a silent data loss: same marker and date,
    *different* value. Two readings six hours apart is an ordinary day on a ward."""
    database_url, sessionmaker = seeded_database
    async with sessionmaker() as db:
        patient = await _chart(db)
        shared = {
            "patient_id": patient.id,
            "marker_name": "Potassium",
            "unit": "mmol/L",
            "sample_date": SAMPLED,
        }
        db.add_all(
            [
                LabResult(**shared, value_numeric=Decimal("5.1"), created_at=EARLIER),
                LabResult(**shared, value_numeric=Decimal("6.4"), created_at=LATER),
            ]
        )
        await db.commit()
        patient_id = patient.id

    _upgraded(database_url)

    async with sessionmaker() as db:
        values = (
            (
                await db.execute(
                    select(LabResult.value_numeric).where(
                        LabResult.patient_id == patient_id,
                        LabResult.is_deleted.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sorted(values) == [Decimal("5.1"), Decimal("6.4")], (
        f"the upgrade retired a distinct reading as a duplicate: {values}"
    )


@pytest.mark.asyncio
async def test_the_dedup_key_survives_a_round_trip_through_the_database(seeded_database):
    """0010's backfill recomputes the key from values it has just SELECTed. If reading a row out
    and keying it again does not reproduce the key the INSERT stored, the backfill relabels every
    row it touches — and the observations it was meant to reconcile stop colliding, silently, in
    the direction that lets the same blood draw be charted twice.

    Only ``lab_observation_key`` and the column types decide this, and the column types are the
    interesting half: ``sample_date`` is ``DateTime(timezone=True)``, so PostgreSQL hands back a
    tz-aware ``datetime`` whatever went in, and ``value_numeric`` is ``Numeric(18, 6)``, so
    ``1.4`` comes back as ``1.400000``. Both are normalised by the key function on purpose; this
    is the test that says so against the real driver rather than against SQLite.
    """
    _database_url, sessionmaker = seeded_database
    async with sessionmaker() as db:
        patient = await _chart(db)
        row = LabResult(
            patient_id=patient.id,
            marker_name="Creatinine",
            value_numeric=Decimal("1.4"),
            unit="mg/dL",
            sample_date=SAMPLED,
        )
        db.add(row)
        await db.commit()
        written = row.dedup_key
        row_id = row.id

    async with sessionmaker() as db:
        read_back = await db.get(LabResult, row_id)
        assert read_back is not None
        recomputed = lab_observation_key(
            read_back.source_document_id,
            read_back.marker_name,
            read_back.value_numeric,
            read_back.sample_date,
        )
    assert recomputed == written, (
        "keying a row read back out of PostgreSQL does not reproduce the key its INSERT stored; "
        "0010's backfill would relabel every row it touched"
    )


# --- 0022: a stop event that was still marked current ------------------------------------------


@pytest.mark.asyncio
async def test_a_stop_event_left_marked_current_is_corrected_by_the_upgrade(seeded_database):
    """``is_current`` on a stop event is the state that made a discontinued drug keep appearing
    on the standing safety board. The migration is a one-line UPDATE; nothing ran it over a row
    until now."""
    database_url, sessionmaker = seeded_database
    async with sessionmaker() as db:
        patient = await _chart(db)
        stopped = MedicationEvent(
            patient_id=patient.id,
            brand_name_raw="Warfarin",
            event_type="stop",
            is_current=True,
            event_date=date(2026, 2, 1),
        )
        started = MedicationEvent(
            patient_id=patient.id,
            brand_name_raw="Metformin",
            event_type="start",
            is_current=True,
            event_date=date(2026, 2, 1),
        )
        db.add_all([stopped, started])
        await db.commit()
        stopped_id, started_id = stopped.id, started.id

    _upgraded(database_url)

    async with sessionmaker() as db:
        stop_row = await db.get(MedicationEvent, stopped_id)
        start_row = await db.get(MedicationEvent, started_id)
    assert stop_row is not None and stop_row.is_current is False, (
        "a stop event survived the upgrade still marked current"
    )
    assert start_row is not None and start_row.is_current is True, (
        "the upgrade cleared is_current on a start event too"
    )


# --- 0024: two answers recorded against one intake question ------------------------------------


@pytest.mark.asyncio
async def test_a_question_answered_twice_keeps_its_first_answer(seeded_database):
    """0024 is the one reconciliation here that deletes outright, because an intake answer is a
    clinician's reply to a generated question rather than a clinical observation, and the
    superseded one has no chart to be missing from. What has to hold is *which* one goes."""
    database_url, sessionmaker = seeded_database
    async with sessionmaker() as db:
        patient = await _chart(db)
        session = ReasoningSession(
            account_id=patient.account_id,
            patient_id=patient.id,
            presenting_complaint="breathless climbing stairs",
        )
        db.add(session)
        await db.flush()
        question = IntakeQuestion(
            session_id=session.id,
            question_text="How long has this been going on?",
        )
        db.add(question)
        await db.flush()
        db.add_all(
            [
                IntakeAnswer(
                    question_id=question.id,
                    session_id=session.id,
                    answer_text="three weeks",
                    created_at=EARLIER,
                ),
                IntakeAnswer(
                    question_id=question.id,
                    session_id=session.id,
                    answer_text="a month",
                    created_at=LATER,
                ),
            ]
        )
        await db.commit()
        question_id = question.id

    _upgraded(database_url)

    async with sessionmaker() as db:
        answers = (
            (
                await db.execute(
                    select(IntakeAnswer.answer_text).where(IntakeAnswer.question_id == question_id)
                )
            )
            .scalars()
            .all()
        )
    assert list(answers) == ["three weeks"], f"expected the first answer to stand, got {answers}"


# --- 0026: the same file uploaded into one chart twice ------------------------------------------


@pytest.mark.asyncio
async def test_the_same_file_uploaded_twice_keeps_the_first_upload_live(seeded_database):
    database_url, sessionmaker = seeded_database
    digest = "b" * 64
    async with sessionmaker() as db:
        patient = await _chart(db)
        shared = {
            "patient_id": patient.id,
            "account_id": patient.account_id,
            "file_name": "lipid-panel.pdf",
            "file_type": "pdf",
            "file_size_bytes": 4096,
            "storage_hash_sha256": digest,
            "extraction_status": "completed",
        }
        first = Document(**shared, storage_path="a/first.pdf", created_at=EARLIER)
        second = Document(**shared, storage_path="a/second.pdf", created_at=LATER)
        db.add_all([first, second])
        await db.commit()
        first_id, second_id = first.id, second.id

    _upgraded(database_url)

    async with sessionmaker() as db:
        kept = await db.get(Document, first_id)
        retired = await db.get(Document, second_id)
    assert kept is not None and kept.is_deleted is False
    assert retired is not None, "the duplicate upload's row was deleted outright"
    assert retired.is_deleted is True
    assert retired.storage_path == "a/second.pdf", (
        "the retired row must keep pointing at its own stored file — the migration's log says "
        "the files themselves are untouched"
    )


@pytest.mark.asyncio
async def test_the_same_file_in_two_different_charts_survives_in_both(seeded_database):
    """The index is per chart. A guideline PDF or a shared lab form attached to two patients is
    not a duplicate, and an upgrade that treated it as one would silently empty a chart."""
    database_url, sessionmaker = seeded_database
    digest = "c" * 64
    async with sessionmaker() as db:
        one = await _chart(db)
        two = await _chart(db)
        for patient in (one, two):
            db.add(
                Document(
                    patient_id=patient.id,
                    account_id=patient.account_id,
                    file_name="referral.pdf",
                    file_type="pdf",
                    file_size_bytes=2048,
                    storage_hash_sha256=digest,
                    storage_path=f"{patient.id}/referral.pdf",
                    extraction_status="completed",
                    created_at=EARLIER,
                )
            )
        await db.commit()

    _upgraded(database_url)

    async with sessionmaker() as db:
        live = (
            (
                await db.execute(
                    select(Document.id).where(
                        Document.storage_hash_sha256 == digest,
                        Document.is_deleted.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(live) == 2, f"the same file in two charts was reconciled to {len(live)} rows"


# --- The upgrade's own promise ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reconciled_table_then_refuses_the_duplicate_it_just_retired(
    duplicate_observations,
):
    """The point of the reconciliation is the index it clears the way for. If the index did not
    end up there, every assertion above would still pass and nothing would be enforced."""
    database_url, sessionmaker, patient_id, _first, _second = duplicate_observations
    _upgraded(database_url)

    async with sessionmaker() as db:
        original = (
            (
                await db.execute(
                    select(LabResult).where(
                        LabResult.patient_id == patient_id,
                        LabResult.is_deleted.is_(False),
                    )
                )
            )
            .scalars()
            .one()
        )
        db.add(
            LabResult(
                patient_id=patient_id,
                marker_name=original.marker_name,
                value_numeric=original.value_numeric,
                unit=original.unit,
                sample_date=original.sample_date,
            )
        )
        with pytest.raises(Exception, match="uq_lab_results_observation|dedup_key"):
            await db.commit()
