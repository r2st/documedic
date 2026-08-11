"""Immutable, hash-chained audit log service (P1-09).

Every clinical action is appended here. Writes are append-only: the service exposes no
update or delete. Each row chains to the previous via SHA-256 so tampering is detectable
(see app.core.audit_hash). A Postgres transaction-level advisory lock serializes appends so
the chain stays linear under concurrency; on SQLite (tests) execution is already serial.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_hash import (
    GENESIS_HASH,
    canonical_payload,
    compute_record_hash,
    verify_chain,
)
from app.models.audit_log import AuditLog

# Arbitrary fixed key for the append advisory lock.
_AUDIT_LOCK_KEY = 4823701

# How many times an append re-reads the chain tail after losing a sequence race. Each retry
# only loses to an append that *succeeded*, so the loop makes progress; the bound exists to
# turn a pathological livelock into a loud failure rather than a hung request.
_MAX_SEQUENCE_ATTEMPTS = 5


def _iso_utc(dt: datetime) -> str:
    """Stable UTC ISO timestamp. SQLite drops tzinfo on round-trip; PG keeps it. Coercing
    naive values to UTC makes the hashed representation identical at write and verify time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _canonical_for(row: AuditLog) -> str:
    """Canonical hashed representation of a stored row, matching what append() hashed."""
    return canonical_payload(
        sequence=row.sequence,
        action=row.action,
        account_id=str(row.account_id) if row.account_id else None,
        patient_id=str(row.patient_id) if row.patient_id else None,
        entity_type=row.entity_type,
        entity_id=str(row.entity_id) if row.entity_id else None,
        payload=row.payload,
        created_at=_iso_utc(row.created_at),
    )


def _chain_entry(row: AuditLog) -> dict[str, Any]:
    """Row rendered as the dict shape :func:`verify_chain` consumes."""
    return {
        "sequence": row.sequence,
        "action": row.action,
        "account_id": str(row.account_id) if row.account_id else None,
        "patient_id": str(row.patient_id) if row.patient_id else None,
        "entity_type": row.entity_type,
        "entity_id": str(row.entity_id) if row.entity_id else None,
        "payload": row.payload,
        "created_at": _iso_utc(row.created_at),
        "prev_hash": row.prev_hash,
        "record_hash": row.record_hash,
    }


@dataclass(frozen=True)
class AuditDraft:
    """One entry to append, before the chain assigns it a sequence and a hash.

    Exists so a caller that produces several entries can hand them over together — see
    :meth:`AuditService.record_many` for why appending them one at a time is expensive.
    """

    action: str
    account_id: uuid.UUID | None = None
    patient_id: uuid.UUID | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    payload: dict = field(default_factory=dict)


class AuditService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _lock(self) -> None:
        """Serialize appends across connections. PostgreSQL only; a no-op elsewhere.

        ``pg_advisory_xact_lock`` is held until the caller's transaction *commits*, not until
        the append returns — there is no unlock. That makes lock hold time a property of the
        whole request rather than of this method: a request that appends early and then does a
        second of clinical work holds the global append lock for that second, and every other
        request that wants to audit anything queues behind it.

        Two consequences the call sites are written around. An entry that describes a *read*
        is appended after the read, immediately before the commit, so the hold is
        microseconds. And a caller with several entries hands them to :meth:`record_many`
        rather than taking the lock once per entry.
        """
        if self.db.bind and self.db.bind.dialect.name == "postgresql":
            await self.db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _AUDIT_LOCK_KEY})

    async def _latest(self) -> AuditLog | None:
        result = await self.db.execute(select(AuditLog).order_by(AuditLog.sequence.desc()).limit(1))
        return result.scalar_one_or_none()

    async def record(
        self,
        *,
        action: str,
        account_id: uuid.UUID | None = None,
        patient_id: uuid.UUID | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        payload: dict | None = None,
    ) -> AuditLog:
        """Append one immutable, hash-chained audit entry. Does not commit.

        A single-entry :meth:`record_many`; the retry and locking behaviour is described there.
        """
        appended = await self.record_many(
            [
                AuditDraft(
                    action=action,
                    account_id=account_id,
                    patient_id=patient_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    payload=payload or {},
                )
            ]
        )
        return appended[0]

    async def record_many(self, drafts: Sequence[AuditDraft]) -> list[AuditLog]:
        """Append several entries as one linked run of the chain. Does not commit.

        The batch is what makes broad auditing affordable. Appending N entries by calling
        :meth:`record` N times costs N advisory-lock round trips, N reads of the chain tail and
        N savepoints, all of them serialized behind the same global lock — and the call sites
        that produce several entries produce them per *clinical item*: one per verified
        suggestion a reasoning run emits, so the cost grew with how much the engine had to say.
        Here the tail is read once and the entries chain to each other in memory, so the run is
        one lock acquisition, one read and one savepoint however many entries it carries.

        An empty batch is a no-op that never touches the lock or the table. That is what lets
        a caller hand over "whatever this request produced" unconditionally without a request
        that produced nothing paying for the global lock anyway.

        Retries on a sequence collision. Assigning the sequence is a read of the current
        maximum followed by an insert, so two appends that overlap can read the same maximum
        and claim the same sequence; the unique constraint then fails one of them. On
        PostgreSQL the advisory lock in :meth:`_lock` usually prevents that, but the lock is
        not the guarantee — it is an optimisation that keeps the common case from colliding.
        The retry is what makes the invariant hold, and it holds on every backend, including
        the SQLite deployments where ``_lock`` does nothing at all.

        Each attempt rebuilds the *whole* batch rather than only the entry that lost. A
        collision means someone else appended, so the sequence and the ``prev_hash`` the batch
        was built on have both moved; every hash in the run is computed from its predecessor,
        so recomputing only the first would leave the rest chained to a hash that is no longer
        where they claim it is.

        The insert runs inside a SAVEPOINT so a failed attempt rolls back only itself. Without
        that, the unique violation would poison the caller's whole transaction — and the
        caller is mid-way through writing the clinical data these entries describe.
        """
        if not drafts:
            return []

        await self._lock()

        for attempt in range(_MAX_SEQUENCE_ATTEMPTS):
            latest = await self._latest()
            sequence = (latest.sequence + 1) if latest else 1
            prev_hash = latest.record_hash if latest else GENESIS_HASH

            entries: list[AuditLog] = []
            for offset, draft in enumerate(drafts):
                created_at = datetime.now(UTC)
                canonical = canonical_payload(
                    sequence=sequence + offset,
                    action=draft.action,
                    account_id=str(draft.account_id) if draft.account_id else None,
                    patient_id=str(draft.patient_id) if draft.patient_id else None,
                    entity_type=draft.entity_type,
                    entity_id=str(draft.entity_id) if draft.entity_id else None,
                    payload=draft.payload,
                    created_at=_iso_utc(created_at),
                )
                record_hash = compute_record_hash(prev_hash, canonical)
                entries.append(
                    AuditLog(
                        sequence=sequence + offset,
                        account_id=draft.account_id,
                        patient_id=draft.patient_id,
                        action=draft.action,
                        entity_type=draft.entity_type,
                        entity_id=draft.entity_id,
                        payload=draft.payload,
                        prev_hash=prev_hash,
                        record_hash=record_hash,
                        created_at=created_at,
                    )
                )
                # The next entry chains to this one, exactly as it would have done had it been
                # appended in its own call straight after this one.
                prev_hash = record_hash

            try:
                async with self.db.begin_nested():
                    self.db.add_all(entries)
                    await self.db.flush()
            except IntegrityError:
                if attempt == _MAX_SEQUENCE_ATTEMPTS - 1:
                    raise
                # The savepoint rollback has already detached every entry; the next attempt
                # builds a fresh batch against the chain tail as it now stands.
                continue
            return entries

        raise AssertionError("unreachable: the loop returns or raises on the final attempt")

    async def list_for_patient(
        self,
        patient_id: uuid.UUID,
        *,
        action: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[AuditLog], int]:
        """Newest-first page of a patient's audit entries, plus the unpaginated total.

        Paginated in two phases -- an inner query that selects only ``sequence``, and an outer
        one that fetches the rows for those sequences. The obvious single-statement form
        (``WHERE patient_id = ? ORDER BY sequence DESC LIMIT n``) makes PostgreSQL choose the
        global unique index on ``sequence`` and scan it backwards, discarding every row that
        belongs to another patient until it happens to collect ``n`` matches. That plan is
        costed as cheaper than ``ix_audit_logs_patient_sequence`` because the planner assumes a
        patient's rows are spread uniformly along ``sequence`` -- but this table is append-only,
        so a patient's entries cluster in the range written while they were being seen. Reading
        the trail of a patient who has been inactive for a while therefore walks everything
        written since.

        Measured on a 622k-row table (EXPLAIN ANALYZE, PostgreSQL 16): for a patient with 20k
        entries the single-statement form scanned 602k rows in 127 ms; this form returns in
        0.14 ms via an index-only scan of ix_audit_logs_patient_sequence with zero heap
        fetches. It degrades with the patient's own history rather than with total table size,
        which matters because audit_logs is never pruned.

        Selecting only the indexed columns is what makes the difference: it turns the composite
        index into a covering one, so its estimated cost drops below the backwards global scan
        and the planner picks it on its own. This is plain SQL -- no dialect-specific hint --
        so SQLite behaves the same.
        """
        from sqlalchemy import func

        base = select(AuditLog).where(AuditLog.patient_id == patient_id)
        if action:
            base = base.where(AuditLog.action == action)

        total = await self.db.scalar(select(func.count()).select_from(base.subquery()))

        page_sequences = select(AuditLog.sequence).where(AuditLog.patient_id == patient_id)
        if action:
            page_sequences = page_sequences.where(AuditLog.action == action)
        page_sequences = (
            page_sequences.order_by(AuditLog.sequence.desc()).limit(limit).offset(offset)
        )

        result = await self.db.execute(
            select(AuditLog)
            .where(
                AuditLog.patient_id == patient_id,
                AuditLog.sequence.in_(page_sequences.scalar_subquery()),
            )
            .order_by(AuditLog.sequence.desc())
        )
        return list(result.scalars().all()), int(total or 0)

    async def verify_patient_chain(self, patient_id: uuid.UUID) -> tuple[int, bool]:
        """Recompute the hash chain for a patient's entries in global sequence order."""
        result = await self.db.execute(
            select(AuditLog)
            .where(AuditLog.patient_id == patient_id)
            .order_by(AuditLog.sequence.asc())
        )
        rows = list(result.scalars().all())
        # Per-patient slice: validate each record's own hash recomputes; chain linkage is
        # validated globally. Here we recompute each record's hash from its stored prev_hash.
        valid = all(
            compute_record_hash(r.prev_hash, _canonical_for(r)) == r.record_hash for r in rows
        )
        return len(rows), valid

    async def verify_full_chain(self) -> tuple[int, bool]:
        """Verify the entire global chain is unbroken (genesis -> latest)."""
        result = await self.db.execute(select(AuditLog).order_by(AuditLog.sequence.asc()))
        entries = [_chain_entry(r) for r in result.scalars().all()]
        return len(entries), verify_chain(entries)
