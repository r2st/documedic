"""Genuinely concurrent writes: two clinicians ingesting into the record at the same time.

Every other suite runs against the shared in-memory fixture in ``conftest``, which uses a
``StaticPool`` — one SQLite connection shared by every session. That is the right trade for
speed, but it means ``asyncio.gather`` over two requests there does not test concurrency at
all: the two sessions interleave on a single connection and produce artefacts (``StaleDataError``,
a unique violation on ``audit_logs.sequence``) that come from the shared connection rather than
from the code under test. A test built on it would fail for a reason production never sees.

So this module builds its own **file-backed** database with a real pool, so each request
genuinely gets its own connection and its own transaction, and concurrent requests contend the
way they do in production. That makes the following assertions mean something:

* two clinicians submitting labs for the same patient at the same time both land — the record
  is append-only, so neither may overwrite the other
* concurrent work on *different* patients never leaks rows across the patient boundary
* the append-only audit log survives it: sequences stay unique and gapless, and the hash chain
  still verifies end to end

One limit worth stating plainly, because it decides what the HTTP-level tests here can prove:
SQLite takes a database-wide write lock, so although the *requests* overlap, their write phases
run one at a time. Instrumenting ``AuditService.record`` during these tests shows a maximum of
one append in flight. So the end-to-end tests below establish that interleaved requests produce
a correct record — they do **not** establish that two simultaneous audit appends are safe.

That guarantee is asserted separately, and at the service layer, by
``test_two_appends_that_read_the_same_chain_tail_both_succeed``: it drives two sessions into
the read-then-insert window on purpose, which is the only way to reach the race from a test.
Before the retry in ``AuditService.record``, that window produced unique violations on
``audit_logs.sequence`` — 9 of that test's 12 concurrent appends failed. PostgreSQL hides it
behind the advisory lock, so it would only ever have shown up on a backend without one.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.seed import seed_all
from app.db.session import get_db
from app.main import create_app
from app.models import Base
from app.models.audit_log import AuditLog
from app.models.lab_result import LabResult
from app.services.audit_service import AuditService

CONCURRENCY = 6


@pytest_asyncio.fixture
async def file_engine(tmp_path: Path):
    """A file-backed SQLite engine, so concurrent requests get independent connections.

    WAL plus a busy timeout is what makes SQLite tolerate concurrent writers at all: without
    them the second writer fails immediately with ``database is locked`` and every test here
    would be measuring that instead of the application's behaviour.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}"
    engine = create_async_engine(url, connect_args={"timeout": 30})

    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.exec_driver_sql("PRAGMA busy_timeout=30000")
        await conn.run_sync(Base.metadata.create_all)

    sm = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with sm() as db:
        await seed_all(db)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def file_sessionmaker(file_engine):
    return async_sessionmaker(file_engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def concurrent_app(file_sessionmaker):
    application = create_app()

    async def _override_get_db():
        async with file_sessionmaker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    application.dependency_overrides[get_db] = _override_get_db
    return application


async def _signed_up_client(app, email: str) -> AsyncClient:
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "password123", "display_name": email},
    )
    assert resp.status_code == 201, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
    return client


@pytest_asyncio.fixture
async def clinicians(concurrent_app):
    """Two clinicians on separate HTTP clients, each with its own access token."""
    first = await _signed_up_client(concurrent_app, "first@example.com")
    resp = await first.post(
        "/api/v1/auth/login", json={"email": "first@example.com", "password": "password123"}
    )
    assert resp.status_code == 200, resp.text
    transport = ASGITransport(app=concurrent_app)
    async with AsyncClient(transport=transport, base_url="http://test") as second:
        # Same account, second session — one practice login used from two rooms, which is the
        # arrangement that actually produces simultaneous writes to one chart.
        second.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        yield first, second
    await first.aclose()


def _lab_document(marker: str, value: float) -> bytes:
    """A synthetic lab report. The %PDF- header satisfies magic-byte sniffing; the body is
    read by the deterministic text parser (no LLM in tests)."""
    return b"%PDF-1.4\nLABS:\n" + f"{marker}: {value} mg/dL (0.6-1.2)\n".encode()


async def _create_patient(client: AsyncClient, name: str) -> str:
    resp = await client.post(
        "/api/v1/patients",
        json={
            "full_name": name,
            "sex": "male",
            "date_of_birth": "1968-05-10",
            "consent_given": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _submit_lab(client: AsyncClient, patient_id: str, marker: str, value: float) -> dict:
    """Upload a lab report and approve it — the only path a result reaches the record by."""
    upload = await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (f"{marker}.pdf", _lab_document(marker, value), "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    approve = await client.post(
        f"/api/v1/patients/{patient_id}/documents/{upload.json()['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approve.status_code == 200, approve.text
    return approve.json()["merged"]


async def _markers_on_record(client: AsyncClient, patient_id: str) -> list[str]:
    resp = await client.get(f"/api/v1/patients/{patient_id}/record")
    assert resp.status_code == 200, resp.text
    return sorted(lab["marker_name"] for lab in resp.json()["lab_results"])


@pytest.mark.asyncio
async def test_concurrent_lab_submissions_for_one_patient_all_land(clinicians, file_engine):
    """Simultaneous submissions must accumulate, not overwrite.

    Lab results are append-only clinical data. A lost write here is a lab result that a
    clinician saw confirmed and that then silently is not on the chart.
    """
    first, second = clinicians
    patient_id = await _create_patient(first, "Concurrent Labs")

    markers = [f"Marker{i}" for i in range(CONCURRENCY)]
    results = await asyncio.gather(
        *(
            _submit_lab(first if i % 2 == 0 else second, patient_id, marker, 1.0 + i)
            for i, marker in enumerate(markers)
        )
    )

    assert all(r["lab_results"] == 1 for r in results), results
    assert await _markers_on_record(first, patient_id) == sorted(markers)


@pytest.mark.asyncio
async def test_concurrent_submissions_for_different_patients_do_not_cross_over(
    clinicians, file_engine
):
    """Interleaved requests must not attribute one patient's result to another."""
    first, second = clinicians
    patient_ids = [await _create_patient(first, f"Patient {i}") for i in range(CONCURRENCY)]

    await asyncio.gather(
        *(
            _submit_lab(first if i % 2 == 0 else second, patient_id, f"OnlyFor{i}", 1.0 + i)
            for i, patient_id in enumerate(patient_ids)
        )
    )

    for i, patient_id in enumerate(patient_ids):
        assert await _markers_on_record(first, patient_id) == [f"OnlyFor{i}"]


@pytest.mark.asyncio
async def test_concurrent_submissions_of_identical_bytes_deduplicate_to_one_document(
    clinicians, file_engine
):
    """Two clinicians uploading the same scan must not produce two documents or two labs.

    Deduplication is a read-then-write on the document hash, so it is exactly the shape that
    breaks under concurrency: both requests can find no existing row before either inserts.
    """
    first, second = clinicians
    patient_id = await _create_patient(first, "Duplicate Scan")
    content = _lab_document("Creatinine", 1.1)

    async def _upload(client: AsyncClient) -> str:
        resp = await client.post(
            f"/api/v1/patients/{patient_id}/documents",
            files={"file": ("rx.pdf", content, "application/pdf")},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    doc_ids = await asyncio.gather(*(_upload(first if i % 2 == 0 else second) for i in range(4)))

    listed = (await first.get(f"/api/v1/patients/{patient_id}/documents")).json()
    assert len({*doc_ids}) == len(listed), (
        f"{len(set(doc_ids))} distinct document ids were returned but {len(listed)} documents "
        "exist — the dedup read-then-write disagrees with what was persisted"
    )


@pytest.mark.asyncio
async def test_the_audit_chain_survives_concurrent_writes(clinicians, file_sessionmaker):
    """Sequences stay unique and gapless, and the hash chain still verifies.

    The audit log is the compliance artefact: a duplicated or skipped sequence, or a broken
    hash link, means the trail can no longer be shown to be complete. Concurrency is the one
    thing that can produce that without anybody editing a row.
    """
    first, second = clinicians
    patient_id = await _create_patient(first, "Audited Concurrently")

    await asyncio.gather(
        *(
            _submit_lab(first if i % 2 == 0 else second, patient_id, f"Audit{i}", 1.0 + i)
            for i in range(CONCURRENCY)
        )
    )

    async with file_sessionmaker() as db:
        sequences = list((await db.execute(select(AuditLog.sequence))).scalars().all())
        assert len(sequences) == len(set(sequences)), "duplicate audit sequence under concurrency"
        assert sorted(sequences) == list(range(1, len(sequences) + 1)), (
            f"audit sequences are not gapless: {sorted(sequences)}"
        )

        count, valid = await AuditService(db).verify_full_chain()
        assert count == len(sequences)
        assert valid, "the audit hash chain no longer verifies after concurrent writes"


@pytest.mark.asyncio
async def test_two_appends_that_read_the_same_chain_tail_both_succeed(file_sessionmaker):
    """The read-then-insert window in ``record`` must not lose an audit entry.

    Assigning a sequence reads the current maximum and then inserts. Two appends that overlap
    in that window claim the same sequence, and the unique constraint rejects one — dropping
    an entry from a compliance log that is supposed to be complete. The ``await asyncio.sleep``
    between the read and the commit is what makes the window reliably reachable; without it
    the scheduler usually happens to serialise the two coroutines and the race hides.

    On PostgreSQL the advisory lock normally closes this window, which is precisely why it
    needs a test that does not depend on the lock: every backend without one, and any future
    change that relaxes the lock to address its throughput cost, relies on the retry instead.
    """
    appended = 12

    async def _append(i: int) -> None:
        async with file_sessionmaker() as db:
            await AuditService(db).record(action=f"concurrent_append_{i}")
            await asyncio.sleep(0)  # yield inside the read-then-insert window
            await db.commit()

    results = await asyncio.gather(*(_append(i) for i in range(appended)), return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"{len(failures)}/{appended} concurrent appends failed: {failures[:3]}"

    async with file_sessionmaker() as db:
        sequences = sorted((await db.execute(select(AuditLog.sequence))).scalars().all())
        assert sequences == list(range(1, appended + 1)), (
            f"expected {appended} gapless sequences, got {sequences} — an append was lost or "
            "reused a sequence"
        )
        count, valid = await AuditService(db).verify_full_chain()
        assert count == appended
        assert valid, (
            "the hash chain does not verify: a retried append kept its original prev_hash "
            "instead of rehashing against the predecessor that beat it"
        )


@pytest.mark.asyncio
async def test_no_lab_result_is_written_twice_under_concurrency(clinicians, file_sessionmaker):
    """A retried or interleaved approval must not duplicate the row it already merged."""
    first, second = clinicians
    patient_id = await _create_patient(first, "No Double Write")

    await asyncio.gather(
        *(
            _submit_lab(first if i % 2 == 0 else second, patient_id, f"Unique{i}", 1.0 + i)
            for i in range(CONCURRENCY)
        )
    )

    async with file_sessionmaker() as db:
        rows = (
            await db.execute(
                select(LabResult.marker_name, func.count())
                .where(LabResult.patient_id == uuid.UUID(patient_id))
                .group_by(LabResult.marker_name)
            )
        ).all()

    duplicated = {marker: n for marker, n in rows if n > 1}
    assert not duplicated, f"markers written more than once: {duplicated}"
