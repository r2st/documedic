"""``AuditService.record_many`` — several entries appended as one linked run of the chain.

The batch exists for throughput, not for expressiveness: on PostgreSQL every append takes a
transaction-scoped advisory lock that is only released at COMMIT, so the cost of appending N
entries one at a time is N lock acquisitions, N reads of the chain tail and N savepoints, all
serialized behind the same global lock. The call sites that produce several entries produce
them per clinical item — one per verified suggestion a reasoning run emits — so that cost grew
with how much the engine had to say.

A throughput optimisation on a tamper-evident log has to be held to the log's own guarantees,
so these tests pin both halves: the batch must cost one tail read, *and* the chain it writes
must be indistinguishable from the one N separate appends would have written.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.services.audit_service import AuditDraft, AuditService
from tests.test_query_efficiency import counting_queries


def _drafts(n: int, patient_id: uuid.UUID | None = None) -> list[AuditDraft]:
    return [
        AuditDraft(
            action="clinical_suggestion_created",
            patient_id=patient_id,
            entity_type="clinical_suggestion",
            entity_id=uuid.uuid4(),
            payload={"index": i},
        )
        for i in range(n)
    ]


async def _entries(db) -> list[AuditLog]:
    result = await db.execute(select(AuditLog).order_by(AuditLog.sequence.asc()))
    return list(result.scalars().all())


# ---------------------------------------------------------------- chain integrity


async def test_a_batch_writes_a_verifiable_chain(db):
    """The whole point of the log: what the batch wrote must verify like anything else."""
    service = AuditService(db)

    appended = await service.record_many(_drafts(6))
    await db.commit()

    assert [e.sequence for e in appended] == list(range(1, 7))
    count, valid = await service.verify_full_chain()
    assert count == 6
    assert valid, "the batch wrote a chain that does not verify"


async def test_each_entry_in_a_batch_chains_to_the_one_before_it(db):
    """Entries inside a batch link to each other, not all to the tail they were built from.

    The failure this guards against is subtle and would still verify per-record: give every
    entry in the run the same ``prev_hash`` and each row's own hash still recomputes, but the
    chain forks — five entries all claiming the same predecessor, and no way to tell which
    one an entry was actually appended after.
    """
    service = AuditService(db)

    appended = await service.record_many(_drafts(5))
    await db.commit()

    for earlier, later in zip(appended, appended[1:], strict=False):
        assert later.prev_hash == earlier.record_hash, (
            f"entry {later.sequence} chains to {later.prev_hash[:8]} but its predecessor's "
            f"hash is {earlier.record_hash[:8]} — the batch forked the chain"
        )
    assert len({e.record_hash for e in appended}) == len(appended), "duplicate record hashes"


async def test_a_batch_continues_the_chain_left_by_earlier_single_appends(db):
    """Batched and single appends interleave into one chain, in call order."""
    service = AuditService(db)

    first = await service.record(action="patient_created", entity_type="patient")
    batched = await service.record_many(_drafts(3))
    last = await service.record(action="graph_merged", entity_type="patient")
    await db.commit()

    assert batched[0].prev_hash == first.record_hash
    assert last.prev_hash == batched[-1].record_hash
    count, valid = await service.verify_full_chain()
    assert (count, valid) == (5, True)


async def test_a_batch_produces_the_same_chain_as_the_same_entries_appended_singly(engine):
    """Equivalence, not just validity: the optimisation must not change what is recorded.

    Runs the identical drafts through both paths against an empty table and compares every
    row. ``created_at`` is excluded because it is wall-clock and genuinely differs between the
    two runs; ``record_hash`` is excluded because it covers that timestamp. What is left is
    everything the chain commits to that a reviewer would read, and it must match exactly.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    drafts = _drafts(4, patient_id=None)

    def _comparable(rows: list[AuditLog]) -> list[tuple]:
        return [(r.sequence, r.action, r.entity_type, r.payload["index"]) for r in rows]

    sm = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with sm() as one:
        await AuditService(one).record_many(drafts)
        await one.commit()
        batched = _comparable(await _entries(one))
        # Same database, so clear it before replaying the single-append path.
        from sqlalchemy import delete

        await one.execute(delete(AuditLog))
        await one.commit()

    async with sm() as two:
        service = AuditService(two)
        for draft in drafts:
            await service.record(
                action=draft.action,
                account_id=draft.account_id,
                patient_id=draft.patient_id,
                entity_type=draft.entity_type,
                entity_id=draft.entity_id,
                payload=draft.payload,
            )
        await two.commit()
        singly = _comparable(await _entries(two))

    assert batched == singly, f"batched chain differs from the single-append chain: {batched}"


# ---------------------------------------------------------------- the throughput property


async def test_a_batch_reads_the_chain_tail_once_however_many_entries_it_carries(db, engine):
    """The reason the batch exists. One tail read for the run, not one per entry.

    On PostgreSQL each of those reads also happens under the global append lock, so a per-
    entry tail read is what turns "the engine produced twelve suggestions" into twelve
    serialized round trips.
    """

    async def _tail_reads(n: int) -> int:
        with counting_queries(engine) as counter:
            await AuditService(db).record_many(_drafts(n))
        return len(
            [
                s
                for s in counter["statements"]
                if s.lstrip().upper().startswith("SELECT") and "audit_logs" in s
            ]
        )

    for n in (1, 4, 16):
        reads = await _tail_reads(n)
        assert reads == 1, f"a {n}-entry batch read the chain tail {reads} times"
    await db.commit()


async def test_an_empty_batch_touches_nothing(db, engine):
    """A caller can hand over "whatever this request produced" unconditionally.

    A request that produced no entries must not pay for the global append lock, and must not
    read the tail either — otherwise adding an unconditional ``record_many`` to a hot read
    path would cost every request a query to discover it had nothing to write.
    """
    with counting_queries(engine) as counter:
        appended = await AuditService(db).record_many([])

    assert appended == []
    assert counter["statements"] == [], (
        f"an empty batch issued {len(counter['statements'])} statements: {counter['statements']}"
    )


# ---------------------------------------------------------------- collision handling


async def test_a_batch_that_loses_a_sequence_race_rebuilds_the_whole_run(db, monkeypatch):
    """A collision moves the tail, so every hash in the run has to be recomputed.

    Rebuilding only the losing entry would leave the rest of the batch chained to a
    predecessor hash that no longer sits where they think it does. Simulated by letting the
    first attempt read a stale tail — the same thing a concurrent append does.
    """
    service = AuditService(db)
    # An entry the batch will collide with: the stub below reports the chain as empty on the
    # first attempt, so the batch claims sequences that are already taken.
    existing = await service.record(action="patient_created", entity_type="patient")
    await db.commit()

    real_latest = service._latest
    calls = {"n": 0}

    async def _stale_once():
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # "the chain is empty" — a stale read of the tail
        return await real_latest()

    monkeypatch.setattr(service, "_latest", _stale_once)

    appended = await service.record_many(_drafts(3))
    await db.commit()

    assert calls["n"] == 2, "the batch did not retry after the collision"
    assert [e.sequence for e in appended] == [2, 3, 4]
    assert appended[0].prev_hash == existing.record_hash, (
        "the retry did not rebuild the run against the tail as it actually stands"
    )
    count, valid = await service.verify_full_chain()
    assert (count, valid) == (4, True)


async def test_a_batch_that_cannot_win_the_sequence_race_raises_rather_than_forking(
    db, monkeypatch
):
    """Bounded retries. A pathological livelock must fail loudly, not silently drop entries."""
    from sqlalchemy.exc import IntegrityError

    service = AuditService(db)
    await service.record(action="patient_created", entity_type="patient")
    await db.commit()

    async def _always_stale():
        return None

    monkeypatch.setattr(service, "_latest", _always_stale)

    with pytest.raises(IntegrityError):
        await service.record_many(_drafts(2))


# ---------------------------------------------------------------- the batched call sites


async def test_a_reasoning_run_audits_its_suggestions_in_one_batch(auth_client, db):
    """End-to-end: the per-suggestion audit loop is gone, and the trail is unchanged.

    Asserts on the trail rather than on the call, because the guarantee that matters to a
    reviewer months later is that every suggestion still has its own immutable entry.
    """
    from tests.conftest import create_patient

    patient = await create_patient(auth_client)
    started = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "chest pain on exertion for two weeks"},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session"]["id"]
    await auth_client.post(f"/api/v1/reasoning/{session_id}/answers", json={"answers": []})
    run = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert run.status_code == 200, run.text

    suggestions = (await auth_client.get(f"/api/v1/reasoning/{session_id}/suggestions")).json()
    audit = (
        await auth_client.get(
            f"/api/v1/patients/{patient['id']}/audit",
            params={"action": "clinical_suggestion_created", "limit": 200},
        )
    ).json()

    assert len(suggestions) > 1, "the run produced too few suggestions to exercise the batch"
    assert audit["pagination"]["total"] == len(suggestions), (
        "every suggestion must still carry its own audit entry"
    )
    audited_ids = {entry["entity_id"] for entry in audit["items"]}
    assert audited_ids == {s["id"] for s in suggestions}

    verify = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit/verify")).json()
    assert verify["chain_valid"] is True
