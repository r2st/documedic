"""The signed-encounter freeze as a property of the database, not of the service.

``tests/test_encounter_lifecycle.py`` proves that ``EncounterService`` refuses to edit a signed
visit. That is the guard a clinician meets, and it is not the guard that matters most. The
question this file asks is the one that outlives the current code: if some *other* writer —
a new code path, a data-repair script, a psql prompt — updates a signed encounter, what happens?

On PostgreSQL the answer is ``trg_encounters_signed_frozen`` (migration 0031), and these tests
issue raw UPDATEs and DELETEs against the table to make sure of it. Nothing here goes through
the ORM's encounter code at all; that is the point. The same reasoning already applies to
``clinical_suggestions`` and ``drug_safety_overrides``, both of which carry their own
immutability triggers rather than relying on nobody writing the wrong query.

The suite runs on SQLite, where migrations return before executing any of this, so these are
PostgreSQL-only and skipped when none is reachable::

    pg_ctl start ...   # see the chain test's docstring
    REQUIRE_POSTGRES=1 pytest -m postgres
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.encounter import Encounter
from app.models.patient import Patient
from app.models.user import Account
from tests.postgres_required import unavailable
from tests.test_migration_chain_postgres import (
    DEFAULT_URL,
    _admin_execute,
    _alembic,
    _swap_database,
)

pytestmark = pytest.mark.postgres

SCRATCH_DATABASE = "documedic_encounter_freeze_test"

VISIT_DATE = date(2026, 8, 14)
SIGNED_AT = datetime(2026, 8, 14, 11, 30, tzinfo=UTC)
ATTESTED_NOTE = "BP 148/92. Bibasal crackles. For furosemide review."


@pytest_asyncio.fixture
async def migrated_database():
    """A scratch database at ``head`` — triggers installed, exactly as a deployment has them."""
    url = os.environ.get("TEST_POSTGRES_URL", DEFAULT_URL)
    try:
        await _admin_execute(url, f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}"')
        await _admin_execute(url, f'CREATE DATABASE "{SCRATCH_DATABASE}"')
    except Exception as exc:  # noqa: BLE001 — unreachable, or no privilege to create one
        unavailable(f"no PostgreSQL to build a scratch database on ({type(exc).__name__}): {exc}")

    database_url = _swap_database(url, SCRATCH_DATABASE)
    try:
        result = _alembic(database_url, "upgrade", "head")
        assert result.returncode == 0, f"could not reach head:\n{result.stderr}"
        engine = create_async_engine(database_url)
        try:
            yield engine, async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
    finally:
        await _admin_execute(url, f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}"')


async def _signed_visit(sessionmaker) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A chart with one signed encounter on it. Returns (patient, account, encounter)."""
    async with sessionmaker() as db:
        account = Account(email=f"freeze-{uuid.uuid4().hex}@example.com", password_hash="x")
        db.add(account)
        await db.flush()
        patient = Patient(
            account_id=account.id,
            full_name="Freeze Test Patient",
            sex="female",
            consent_given=True,
        )
        db.add(patient)
        await db.flush()
        encounter = Encounter(
            patient_id=patient.id,
            encounter_date=VISIT_DATE,
            encounter_type="outpatient",
            clinician_notes=ATTESTED_NOTE,
            status="signed",
            signed_at=SIGNED_AT,
            signed_by_account_id=account.id,
        )
        db.add(encounter)
        await db.commit()
        return patient.id, account.id, encounter.id


async def _raw(engine, statement: str, **params):
    async with engine.begin() as conn:
        return await conn.execute(text(statement), params)


# --- the freeze -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_raw_update_of_a_signed_note_is_refused_by_the_database(migrated_database):
    """The claim, stated where no application code is involved."""
    engine, sessionmaker = migrated_database
    _patient_id, _account_id, encounter_id = await _signed_visit(sessionmaker)

    with pytest.raises(DBAPIError) as caught:
        await _raw(
            engine,
            "UPDATE encounters SET clinician_notes = :note WHERE id = :id",
            note="Rewritten by hand months later.",
            id=encounter_id,
        )
    assert "immutable" in str(caught.value).lower()


@pytest.mark.asyncio
async def test_the_note_is_exactly_as_attested_after_the_refusal(migrated_database):
    engine, sessionmaker = migrated_database
    _patient_id, _account_id, encounter_id = await _signed_visit(sessionmaker)

    with pytest.raises(DBAPIError):
        await _raw(
            engine,
            "UPDATE encounters SET clinician_notes = 'x' WHERE id = :id",
            id=encounter_id,
        )

    result = await _raw(
        engine, "SELECT clinician_notes FROM encounters WHERE id = :id", id=encounter_id
    )
    assert result.scalar_one() == ATTESTED_NOTE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("encounter_date", "2020-01-01"),
        ("encounter_type", "emergency"),
        ("presenting_complaint", "Something else entirely"),
        ("clinician_notes", "Rewritten"),
    ],
)
async def test_every_clinical_column_is_frozen_not_just_the_notes(migrated_database, column, value):
    """A freeze that covered only the free text would leave the visit's *date* editable, which
    is enough on its own to move a finding to a consultation it was not recorded at."""
    engine, sessionmaker = migrated_database
    _patient_id, _account_id, encounter_id = await _signed_visit(sessionmaker)

    with pytest.raises(DBAPIError):
        await _raw(
            engine,
            f"UPDATE encounters SET {column} = :value WHERE id = :id",
            value=value,
            id=encounter_id,
        )


@pytest.mark.asyncio
async def test_the_signature_itself_cannot_be_moved(migrated_database):
    """Who attested and when is as much a part of the record as what they attested to."""
    engine, sessionmaker = migrated_database
    _patient_id, _account_id, encounter_id = await _signed_visit(sessionmaker)

    with pytest.raises(DBAPIError):
        await _raw(
            engine,
            "UPDATE encounters SET signed_at = now() WHERE id = :id",
            id=encounter_id,
        )


@pytest.mark.asyncio
async def test_a_signed_visit_cannot_be_walked_back_to_a_draft(migrated_database):
    """The edit that would otherwise get in through the side door: unfreeze, then rewrite."""
    engine, sessionmaker = migrated_database
    _patient_id, _account_id, encounter_id = await _signed_visit(sessionmaker)

    with pytest.raises(DBAPIError) as caught:
        await _raw(
            engine,
            "UPDATE encounters SET status = 'draft' WHERE id = :id",
            id=encounter_id,
        )
    assert "cannot return to" in str(caught.value)


@pytest.mark.asyncio
async def test_a_signed_visit_cannot_be_deleted(migrated_database):
    """A signed note removed from the table takes with it the only evidence of what the chart
    said — and the audit trail's hash chain references the row."""
    engine, sessionmaker = migrated_database
    _patient_id, _account_id, encounter_id = await _signed_visit(sessionmaker)

    with pytest.raises(DBAPIError) as caught:
        await _raw(engine, "DELETE FROM encounters WHERE id = :id", id=encounter_id)
    assert "cannot be deleted" in str(caught.value)


# --- what the freeze must NOT stop -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_amendment_transition_still_works(migrated_database):
    """``signed`` -> ``amended`` is the one status move a signed row must still make. A freeze
    that blocked it would make the amendment mechanism unimplementable."""
    engine, sessionmaker = migrated_database
    _patient_id, _account_id, encounter_id = await _signed_visit(sessionmaker)

    await _raw(
        engine,
        "UPDATE encounters SET status = 'amended', amended_at = now() WHERE id = :id",
        id=encounter_id,
    )
    result = await _raw(engine, "SELECT status FROM encounters WHERE id = :id", id=encounter_id)
    assert result.scalar_one() == "amended"


@pytest.mark.asyncio
async def test_withdrawing_a_chart_still_works_on_one_holding_signed_visits(migrated_database):
    """Soft delete is an UPDATE, and it has to keep working: withdrawal is the chart leaving
    use, not the erasure of a note somebody attested to."""
    engine, sessionmaker = migrated_database
    _patient_id, _account_id, encounter_id = await _signed_visit(sessionmaker)

    await _raw(
        engine,
        "UPDATE encounters SET is_deleted = true, deleted_at = now() WHERE id = :id",
        id=encounter_id,
    )
    result = await _raw(engine, "SELECT is_deleted FROM encounters WHERE id = :id", id=encounter_id)
    assert result.scalar_one() is True


@pytest.mark.asyncio
async def test_an_unsigned_draft_is_freely_editable(migrated_database):
    """The freeze is about attestation, not about encounters. A draft is a working note."""
    engine, sessionmaker = migrated_database
    async with sessionmaker() as db:
        account = Account(email=f"draft-{uuid.uuid4().hex}@example.com", password_hash="x")
        db.add(account)
        await db.flush()
        patient = Patient(account_id=account.id, full_name="Draft Patient", consent_given=True)
        db.add(patient)
        await db.flush()
        encounter = Encounter(patient_id=patient.id, encounter_date=VISIT_DATE, status="draft")
        db.add(encounter)
        await db.commit()
        encounter_id = encounter.id

    await _raw(
        engine,
        "UPDATE encounters SET clinician_notes = 'Still working on it' WHERE id = :id",
        id=encounter_id,
    )
    result = await _raw(
        engine, "SELECT clinician_notes FROM encounters WHERE id = :id", id=encounter_id
    )
    assert result.scalar_one() == "Still working on it"


# --- the constraints around the freeze ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_signed_row_without_a_signer_is_refused(migrated_database):
    """``ck_encounters_signature_complete``. A signed encounter with no signer reads, on any
    screen and in any export, exactly like one nobody attested to."""
    engine, sessionmaker = migrated_database
    patient_id, _account_id, _encounter_id = await _signed_visit(sessionmaker)

    with pytest.raises(IntegrityError):
        await _raw(
            engine,
            "INSERT INTO encounters (id, patient_id, encounter_date, status, created_at, "
            "updated_at, is_deleted, extraction_confidence) "
            "VALUES (gen_random_uuid(), :patient_id, :when, 'signed', now(), now(), false, '{}')",
            patient_id=patient_id,
            when=VISIT_DATE,
        )


@pytest.mark.asyncio
async def test_an_amendment_without_a_reason_is_refused(migrated_database):
    """``ck_encounters_amendment_complete``. Half the pair is a correction whose reason nobody
    can read back."""
    engine, sessionmaker = migrated_database
    patient_id, _account_id, encounter_id = await _signed_visit(sessionmaker)

    with pytest.raises(IntegrityError):
        await _raw(
            engine,
            "INSERT INTO encounters (id, patient_id, encounter_date, status, "
            "amends_encounter_id, created_at, updated_at, is_deleted, extraction_confidence) "
            "VALUES (gen_random_uuid(), :patient_id, :when, 'draft', :target, now(), now(), "
            "false, '{}')",
            patient_id=patient_id,
            when=VISIT_DATE,
            target=encounter_id,
        )


@pytest.mark.asyncio
async def test_only_one_signed_amendment_of_a_visit_can_exist(migrated_database):
    """``uq_encounters_one_signed_amendment``, holding independently of the row lock in
    ``EncounterService.sign``. Two successors to one consultation, and the chart has no rule for
    which one a clinician is reading."""
    engine, sessionmaker = migrated_database
    patient_id, account_id, encounter_id = await _signed_visit(sessionmaker)

    insert = (
        "INSERT INTO encounters (id, patient_id, encounter_date, status, signed_at, "
        "signed_by_account_id, amends_encounter_id, amendment_reason, created_at, updated_at, "
        "is_deleted, extraction_confidence) "
        "VALUES (gen_random_uuid(), :patient_id, :when, 'signed', now(), :account_id, :target, "
        "'Correcting the transcribed potassium value.', now(), now(), false, '{}')"
    )
    params = {
        "patient_id": patient_id,
        "when": VISIT_DATE,
        "account_id": account_id,
        "target": encounter_id,
    }
    await _raw(engine, insert, **params)
    with pytest.raises(IntegrityError):
        await _raw(engine, insert, **params)


@pytest.mark.asyncio
async def test_two_competing_drafts_are_allowed(migrated_database):
    """The other half of that index: two clinicians may both *open* an amendment. Only one can
    finish it. Constraining the drafts would lose one clinician's typing to the other's timing.
    """
    engine, sessionmaker = migrated_database
    patient_id, _account_id, encounter_id = await _signed_visit(sessionmaker)

    insert = (
        "INSERT INTO encounters (id, patient_id, encounter_date, status, amends_encounter_id, "
        "amendment_reason, created_at, updated_at, is_deleted, extraction_confidence) "
        "VALUES (gen_random_uuid(), :patient_id, :when, 'draft', :target, "
        "'Correcting the transcribed potassium value.', now(), now(), false, '{}')"
    )
    params = {"patient_id": patient_id, "when": VISIT_DATE, "target": encounter_id}
    await _raw(engine, insert, **params)
    await _raw(engine, insert, **params)

    result = await _raw(
        engine,
        "SELECT count(*) FROM encounters WHERE amends_encounter_id = :target",
        target=encounter_id,
    )
    assert result.scalar_one() == 2
