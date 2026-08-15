"""The overflow fixes, proved against a real PostgreSQL rather than against SQLite.

The two bugs these cover are invisible to the rest of the suite by construction. SQLite stores
any string in a ``VARCHAR(n)`` and any magnitude in a ``NUMERIC(p, s)``; PostgreSQL raises
``value too long for type character varying(n)`` and ``numeric field overflow``. Every
deployment runs on PostgreSQL, so "the tests pass" never said anything about these paths.

``tests/test_column_bounds.py`` reproduces the limits from the SQLite side and runs everywhere;
this module is the end-to-end confirmation that the reproduction is faithful, and it runs only
when a PostgreSQL is actually reachable:

    docker compose up -d postgres
    pytest tests/test_postgres_column_bounds.py

Point it elsewhere with ``TEST_POSTGRES_URL``. The fixture builds the schema in a throwaway
PostgreSQL schema and drops it afterwards, so it never touches an existing ``public``.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.graph_service import GraphService
from tests.column_fit import assert_fits_columns
from tests.postgres_required import unavailable

pytestmark = pytest.mark.postgres

DEFAULT_URL = "postgresql+asyncpg://aether:aether@localhost:55432/aether_clinician"
TEST_SCHEMA = "documedic_column_bounds_test"

OVERLONG = "X" * 900
OVERFLOWING_VALUE = "99999999999999999999999.5"


@pytest_asyncio.fixture
async def pg_session():
    """A session against a real PostgreSQL, or a skip when none is reachable.

    Everything lives in its own schema so a developer pointing ``TEST_POSTGRES_URL`` at a
    database that already has data cannot lose it — the fixture creates the schema, puts the
    whole model metadata in it via ``search_path``, and drops it on the way out.
    """
    url = os.environ.get("TEST_POSTGRES_URL", DEFAULT_URL)
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": TEST_SCHEMA}},
        poolclass=None,
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{TEST_SCHEMA}"'))
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # noqa: BLE001 - any connect/DDL failure means "no PostgreSQL here"
        await engine.dispose()
        unavailable(f"no reachable PostgreSQL at {url}: {type(exc).__name__}: {exc}")

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with sessionmaker() as session:
            yield session
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))
        await engine.dispose()


async def _patient(db) -> Patient:
    account = Account(email=f"{uuid.uuid4()}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id, full_name="Overflow Probe", sex="male", consent_given=True
    )
    db.add(patient)
    await db.flush()
    return patient


# --------------------------------------------------------------------------------------
# The harness first: prove PostgreSQL really does reject what SQLite accepts
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_rejects_an_overlong_string(pg_session):
    """Without this, every test below could pass because nothing was being enforced."""
    patient = await _patient(pg_session)
    pg_session.add(Condition(patient_id=patient.id, condition_name=OVERLONG, status="active"))

    with pytest.raises(DBAPIError, match="value too long"):
        await pg_session.flush()
    await pg_session.rollback()


@pytest.mark.asyncio
async def test_postgres_rejects_an_out_of_range_numeric(pg_session):
    patient = await _patient(pg_session)
    pg_session.add(
        LabResult(
            patient_id=patient.id,
            dedup_key=uuid.uuid4().hex,
            marker_name="Potassium",
            value_numeric=Decimal(OVERFLOWING_VALUE),
        )
    )

    with pytest.raises(DBAPIError, match="numeric field overflow"):
        await pg_session.flush()
    await pg_session.rollback()


# --------------------------------------------------------------------------------------
# The merge, against the database that actually enforces the columns
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pathological_extraction_commits_to_postgres(pg_session):
    """The R27 report, end to end: both overflows on one document, plus the enum values.

    Before the fix this raised at ``flush`` and the 500 rolled back *every* entity on the
    document, not just the two unreadable fields.
    """
    patient = await _patient(pg_session)

    counts = await GraphService(pg_session).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {
                "entity_type": "medication",
                "fields": {
                    "brand_name_raw": OVERLONG,  # the 900-char drug name
                    "generic_name": OVERLONG,
                    "dose": OVERLONG,
                    "event_type": "titrating up",  # not a listed event_type
                },
            },
            {
                "entity_type": "lab_result",
                "fields": {
                    "marker_name": OVERLONG,
                    "value_numeric": OVERFLOWING_VALUE,  # the 23-digit lab value
                    "unit": OVERLONG,
                    "reference_range_high": "inf",
                },
            },
            {
                "entity_type": "condition",
                "fields": {
                    "condition_name": OVERLONG,
                    "status": "well controlled",
                    "severity": "quite bad",
                },
            },
            {
                "entity_type": "allergy",
                "fields": {
                    "allergen_name": OVERLONG,
                    "allergen_type": "pharmaceutical",
                    "severity": "very bad",
                },
            },
            # A clean row on the same document: the point of coercing rather than refusing is
            # that this one still reaches the chart.
            {
                "entity_type": "medication",
                "fields": {"brand_name_raw": "Crocin", "dose": "650", "dose_unit": "mg"},
            },
        ],
    )
    # Written as "these counts, and nothing else merged" rather than as one dict comparison.
    # The dict form was `== {"medications": 2, "lab_results": 1, "conditions": 1, "allergies": 1}`
    # and it went stale the moment ``merge_entities`` started reporting encounters, which is a
    # key this document has none of — a failure that says nothing about column bounds. It stayed
    # stale because this whole module skips wherever no PostgreSQL is reachable.
    seeded = {"medications": 2, "lab_results": 1, "conditions": 1, "allergies": 1}
    assert {k: counts[k] for k in seeded} == seeded
    assert not {k: v for k, v in counts.items() if k not in seeded and v}, (
        f"the document merged something it does not contain: {counts}"
    )

    await pg_session.commit()  # the real gate: PostgreSQL validates every column here

    for model in (MedicationEvent, LabResult, Condition, Allergy):
        rows = (await pg_session.execute(select(model))).scalars().all()
        assert rows, f"{model.__name__} did not persist"
        assert_fits_columns(*rows)

    # The readable prescription line survived the document that contained the unreadable ones.
    meds = (await pg_session.execute(select(MedicationEvent))).scalars().all()
    assert any(m.brand_name_raw == "Crocin" for m in meds)


@pytest.mark.asyncio
async def test_overflowing_lab_value_persists_as_qualitative_in_postgres(pg_session):
    patient = await _patient(pg_session)
    await GraphService(pg_session).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {
                "entity_type": "lab_result",
                "fields": {
                    "marker_name": "Potassium",
                    "value_numeric": OVERFLOWING_VALUE,
                    "unit": "mmol/L",
                    "reference_range_low": "3.5",
                    "reference_range_high": "5.0",
                },
            }
        ],
    )
    await pg_session.commit()

    lab = (await pg_session.execute(select(LabResult))).scalar_one()
    assert lab.value_numeric is None
    assert OVERFLOWING_VALUE in lab.value_text
    assert lab.is_abnormal is None


@pytest.mark.asyncio
async def test_values_at_the_column_boundary_round_trip_through_postgres(pg_session):
    """The largest value Numeric(18, 6) holds must survive, not just be rejected safely.

    A ceiling set one digit too low would make every test above pass while quietly dropping
    readable lab values, so this pins the other side of the boundary.
    """
    patient = await _patient(pg_session)
    await GraphService(pg_session).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {
                "entity_type": "lab_result",
                "fields": {"marker_name": "Boundary", "value_numeric": "999999999999.999999"},
            },
            {
                "entity_type": "lab_result",
                "fields": {"marker_name": "Ordinary", "value_numeric": "1.2"},
            },
        ],
    )
    await pg_session.commit()

    by_marker = {
        lab.marker_name: lab
        for lab in (await pg_session.execute(select(LabResult))).scalars().all()
    }
    assert by_marker["Boundary"].value_numeric == Decimal("999999999999.999999")
    assert by_marker["Ordinary"].value_numeric == Decimal("1.200000")


@pytest.mark.asyncio
async def test_overlong_strings_are_stored_at_exactly_the_column_width(pg_session):
    patient = await _patient(pg_session)
    await GraphService(pg_session).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {"entity_type": "allergy", "fields": {"allergen_name": OVERLONG}},
            {"entity_type": "condition", "fields": {"condition_name": OVERLONG}},
        ],
    )
    await pg_session.commit()

    allergy = (await pg_session.execute(select(Allergy))).scalar_one()
    condition = (await pg_session.execute(select(Condition))).scalar_one()
    assert len(allergy.allergen_name) == Allergy.__table__.c.allergen_name.type.length
    assert len(condition.condition_name) == Condition.__table__.c.condition_name.type.length
