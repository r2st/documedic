"""Chain verification has to stay bounded as a patient's trail grows.

``audit_logs`` is append-only and never pruned, and it records every PHI *read* as well as
every write, so the trail of a patient under long-term follow-up runs to tens of thousands of
rows. ``verify_patient_chain`` used to read all of it in one statement, hydrate every row into
an ORM instance, and hash the lot in a single expression — unbounded memory, and the event loop
held for the whole pass. That is the shape of the bcrypt and blob-I/O defects: unbounded work on
the one thread the worker serves everybody from.

It reads in ``_VERIFY_BATCH_SIZE`` batches now, walking a ``sequence`` cursor forwards. These
tests pin the properties that batching is easy to get wrong on: nothing may be skipped at a
batch boundary, tampering in *any* batch has to be caught (not just the first), the count has to
stay the patient's own, and the loop has to keep running throughout.
"""

from __future__ import annotations

import asyncio
import math
import uuid

import pytest
from sqlalchemy import func, select, text

from app.models.audit_log import AuditLog
from app.services import audit_service as audit_module
from app.services.audit_service import _VERIFY_BATCH_SIZE, AuditDraft, AuditService
from tests.conftest import create_patient

# Two full batches and a partial one: enough to exercise the boundary from both sides -- a batch
# that fills exactly and a final one that does not -- without making the suite slow.
ENTRIES = _VERIFY_BATCH_SIZE * 2 + 7


async def _fill_trail(db, patient_id: uuid.UUID, count: int = ENTRIES) -> None:
    """Append ``count`` entries to one patient's trail through the real chaining code."""
    await AuditService(db).record_many(
        [
            AuditDraft(
                action="record_viewed",
                patient_id=patient_id,
                entity_type="patient",
                payload={"n": i},
            )
            for i in range(count)
        ]
    )
    await db.commit()


async def _patient_id(client) -> uuid.UUID:
    return uuid.UUID((await create_patient(client))["id"])


async def test_a_trail_spanning_several_batches_is_verified_in_full(db, auth_client):
    patient_id = await _patient_id(auth_client)
    before = await db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.patient_id == patient_id)
    )
    await _fill_trail(db, patient_id)

    checked, valid = await AuditService(db).verify_patient_chain(patient_id)

    assert valid is True
    # Every row, not just the first batch -- the bug a cursor-walk is most likely to have.
    assert checked == int(before or 0) + ENTRIES


@pytest.mark.parametrize("position", ["first", "boundary", "last"])
async def test_tampering_is_caught_wherever_it_sits_in_the_trail(db, auth_client, position):
    """A verification that stopped after one batch would pass two of these three."""
    patient_id = await _patient_id(auth_client)
    await _fill_trail(db, patient_id)
    sequences = list(
        (
            await db.execute(
                select(AuditLog.sequence)
                .where(AuditLog.patient_id == patient_id)
                .order_by(AuditLog.sequence.asc())
            )
        ).scalars()
    )
    target = {
        "first": sequences[0],
        # The first row of the second batch: the one a fencepost error skips.
        "boundary": sequences[_VERIFY_BATCH_SIZE],
        "last": sequences[-1],
    }[position]

    await db.execute(
        text("UPDATE audit_logs SET payload = :p WHERE sequence = :s"),
        {"p": '{"n": "TAMPERED"}', "s": target},
    )
    await db.commit()

    checked, valid = await AuditService(db).verify_patient_chain(patient_id)

    assert valid is False
    # Tamper evidence covers the whole trail rather than stopping at the first discrepancy --
    # an operator needs to know how much was checked, not merely that something was wrong.
    assert checked == len(sequences)


async def test_only_this_patient_s_entries_are_counted(db, auth_client):
    """The cursor filters on patient *and* sequence; dropping the first would count everything."""
    mine = await _patient_id(auth_client)
    theirs = await _patient_id(auth_client)
    await _fill_trail(db, theirs, count=_VERIFY_BATCH_SIZE + 3)
    mine_total = await db.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.patient_id == mine)
    )

    checked, valid = await AuditService(db).verify_patient_chain(mine)

    assert valid is True
    assert checked == int(mine_total or 0)


async def test_an_empty_trail_verifies_as_valid(db):
    """A patient id with no entries: no rows, nothing to disprove, one query and out."""
    assert await AuditService(db).verify_patient_chain(uuid.uuid4()) == (0, True)


async def test_no_single_read_pulls_more_than_one_batch(db, auth_client, monkeypatch):
    """Peak resident rows are what batching is for, and a round-trip count is how to see it.

    One statement per batch plus the empty one that ends the walk. The version this replaced
    did the whole trail in a single statement, so this is the assertion that separates them.
    """
    patient_id = await _patient_id(auth_client)
    await _fill_trail(db, patient_id)

    reads = 0
    real_execute = type(db).execute

    async def _execute(self, statement, *args, **kwargs):
        nonlocal reads
        reads += 1
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(type(db), "execute", _execute)
    checked, valid = await AuditService(db).verify_patient_chain(patient_id)

    assert valid is True
    assert reads == math.ceil(checked / _VERIFY_BATCH_SIZE) + 1, (
        f"{checked} entries read in {reads} statements -- expected one per batch plus the "
        f"empty read that ends the walk"
    )


async def test_the_loop_keeps_running_part_way_through_the_hashing(db, auth_client, monkeypatch):
    """The point of the change, stated as behaviour rather than as a batch size.

    Deliberately not "did the loop tick at all during the call" — it always did, because reading
    the rows is itself an await, and that assertion passes just as happily on the version that
    then hashed the entire trail in one uninterrupted expression. What has to be true is that
    the loop advances *between* hashes: other requests make progress while a long trail is being
    checked, rather than waiting for the whole of it.
    """
    patient_id = await _patient_id(auth_client)
    await _fill_trail(db, patient_id)

    ticks = 0
    ticks_at_each_hash: list[int] = []
    real_hash = audit_module.compute_record_hash

    def _hash(prev_hash: str, canonical: str) -> str:
        ticks_at_each_hash.append(ticks)
        return real_hash(prev_hash, canonical)

    monkeypatch.setattr(audit_module, "compute_record_hash", _hash)

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    ticker = asyncio.create_task(tick())
    _checked, valid = await AuditService(db).verify_patient_chain(patient_id)
    ticker.cancel()

    assert valid is True
    assert len(ticks_at_each_hash) > _VERIFY_BATCH_SIZE, "not every entry was hashed"
    assert len(set(ticks_at_each_hash)) > 1, (
        "the event loop did not advance once between the first hash and the last -- the whole "
        "trail was hashed without yielding"
    )


async def test_the_endpoint_reports_the_batched_count(db, auth_client):
    """End to end: the route's ``entries_checked`` is the whole trail, not one batch."""
    patient = await create_patient(auth_client)
    await _fill_trail(db, uuid.UUID(patient["id"]))

    body = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit/verify")).json()

    assert body["chain_valid"] is True
    assert body["entries_checked"] > _VERIFY_BATCH_SIZE
