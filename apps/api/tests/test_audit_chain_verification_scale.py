"""Chain verification has to stay bounded as the log grows.

``audit_logs`` is append-only and never pruned, and it records every PHI *read* as well as
every write, so the trail of a patient under long-term follow-up runs to tens of thousands of
rows — and the global table to that times every patient. Both verifications used to read their
whole slice in one statement, hydrate every row into an ORM instance, and hash the lot in a
single expression — unbounded memory, and the event loop held for the whole pass. That is the
shape of the bcrypt and blob-I/O defects: unbounded work on the one thread the worker serves
everybody from. Both are reachable from live endpoints (``/patients/{id}/audit/verify``, and
``/regulatory/samd-dossier`` for the global one).

They read in ``_VERIFY_BATCH_SIZE`` batches now, walking a ``sequence`` cursor forwards. These
tests pin the properties that batching is easy to get wrong on: nothing may be skipped at a
batch boundary, tampering in *any* batch has to be caught (not just the first), the count has to
stay the right slice's, the loop has to keep running throughout — and for the global chain, the
link between the last row of one batch and the first of the next has to survive the boundary.
"""

from __future__ import annotations

import asyncio
import math
import uuid

import pytest
from sqlalchemy import func, select, text

from app.core.audit_hash import (
    GENESIS_HASH,
    canonical_payload,
    compute_record_hash,
    verify_chain,
    verify_chain_from,
)
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


# --- the global chain -------------------------------------------------------------------------


async def test_the_global_chain_survives_the_batch_boundary(db, auth_client):
    """The linkage check is the whole point of the global walk, and batching is what breaks it.

    Each entry's ``prev_hash`` has to equal the previous row's ``record_hash`` — across a batch
    boundary as much as within one. A walk that restarted from genesis on every batch, or simply
    forgot the last hash of the previous one, fails here and nowhere else: one batch's worth of
    entries verifies perfectly well on its own.
    """
    await _fill_trail(db, await _patient_id(auth_client))
    total = await db.scalar(select(func.count()).select_from(AuditLog))

    checked, valid = await AuditService(db).verify_full_chain()

    assert valid is True
    assert checked == int(total or 0)
    assert checked > _VERIFY_BATCH_SIZE, "the fixture did not produce a multi-batch chain"


async def test_a_link_broken_at_the_batch_boundary_is_caught(db, auth_client):
    """Repoint the first row of the second batch. Its own hash still recomputes; the link does
    not — which is exactly the tamper a per-batch restart would wave through."""
    await _fill_trail(db, await _patient_id(auth_client))
    sequences = list(
        (await db.execute(select(AuditLog.sequence).order_by(AuditLog.sequence.asc()))).scalars()
    )
    await db.execute(
        text("UPDATE audit_logs SET prev_hash = :h WHERE sequence = :s"),
        {"h": "f" * 64, "s": sequences[_VERIFY_BATCH_SIZE]},
    )
    await db.commit()

    checked, valid = await AuditService(db).verify_full_chain()

    assert valid is False
    assert checked == len(sequences)


async def test_the_global_walk_reads_one_batch_at_a_time(db, auth_client, monkeypatch):
    await _fill_trail(db, await _patient_id(auth_client))

    reads = 0
    real_execute = type(db).execute

    async def _execute(self, statement, *args, **kwargs):
        nonlocal reads
        reads += 1
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(type(db), "execute", _execute)
    checked, valid = await AuditService(db).verify_full_chain()

    assert valid is True
    assert reads == math.ceil(checked / _VERIFY_BATCH_SIZE) + 1


async def test_verify_chain_from_resumes_where_it_left_off():
    """The primitive the batching rests on, checked without a database in the way.

    Splitting a chain and feeding it through in two calls has to agree with one call over the
    whole of it — and a run fed the wrong starting hash has to be rejected.
    """
    entries = _synthetic_chain(6)

    whole, tail_hash = verify_chain_from(entries, GENESIS_HASH)
    first_half, carried = verify_chain_from(entries[:3], GENESIS_HASH)
    second_half, resumed_tail = verify_chain_from(entries[3:], carried)

    assert whole is True
    assert first_half is True and second_half is True
    assert resumed_tail == tail_hash
    assert verify_chain(entries) is whole
    # The carried hash is load-bearing: start the second half from genesis instead and the link
    # into it no longer checks out.
    assert verify_chain_from(entries[3:], GENESIS_HASH)[0] is False


def _synthetic_chain(n: int) -> list[dict]:
    """``n`` correctly-linked entries, built the way ``append`` builds them."""
    entries: list[dict] = []
    prev = GENESIS_HASH
    for i in range(n):
        entry = {
            "sequence": i + 1,
            "action": "record_viewed",
            "account_id": None,
            "patient_id": None,
            "entity_type": "patient",
            "entity_id": None,
            "payload": {"n": i},
            "created_at": f"2026-08-14T09:{i:02d}:00+00:00",
            "prev_hash": prev,
        }
        entry["record_hash"] = compute_record_hash(prev, canonical_payload(**_hashed(entry)))
        prev = entry["record_hash"]
        entries.append(entry)
    return entries


def _hashed(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k not in {"prev_hash", "record_hash"}}
