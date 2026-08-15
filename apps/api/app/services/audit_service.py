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
    verify_chain_from,
    verify_patient_chain_from,
)
from app.models.audit_log import AuditLog

# Arbitrary fixed key for the append advisory lock.
_AUDIT_LOCK_KEY = 4823701

# How many times an append re-reads the chain tail after losing a sequence race. Each retry
# only loses to an append that *succeeded*, so the loop makes progress; the bound exists to
# turn a pathological livelock into a loud failure rather than a hung request.
_MAX_SEQUENCE_ATTEMPTS = 5

# How many audit rows a chain verification reads and hashes between awaits. Large enough that
# the per-batch round trip is amortised over real work, small enough that the event loop is
# never held for more than a few milliseconds at a time.
_VERIFY_BATCH_SIZE = 500


def _iso_utc(dt: datetime) -> str:
    """Stable UTC ISO timestamp. SQLite drops tzinfo on round-trip; PG keeps it. Coercing
    naive values to UTC makes the hashed representation identical at write and verify time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _canonical_for(row: Any) -> str:
    """Canonical hashed representation of a stored row, matching what append() hashed.

    ``row`` is anything carrying the hashed columns: an ``AuditLog`` entity, or a ``Row`` from
    the columns-only select :meth:`AuditService.verify_patient_chain` uses. Deliberately one
    function for both — the value it produces has to be byte-identical to what ``append``
    hashed, and a second implementation of it is a chain that silently stops verifying.
    """
    return canonical_payload(
        sequence=row.sequence,
        action=row.action,
        account_id=str(row.account_id) if row.account_id else None,
        patient_id=str(row.patient_id) if row.patient_id else None,
        entity_type=row.entity_type,
        entity_id=str(row.entity_id) if row.entity_id else None,
        payload=row.payload,
        created_at=_iso_utc(row.created_at),
        patient_prev_hash=row.patient_prev_hash,
    )


# The columns a chain check reads. Selected explicitly rather than mapping whole entities: the
# verifications below walk the table, nothing they do needs a mapped object, and each one would
# cost an instrumented instance and an identity-map entry per row for a value read once.
_CHAIN_COLUMNS = (
    AuditLog.sequence,
    AuditLog.action,
    AuditLog.account_id,
    AuditLog.patient_id,
    AuditLog.entity_type,
    AuditLog.entity_id,
    AuditLog.payload,
    AuditLog.created_at,
    AuditLog.prev_hash,
    AuditLog.record_hash,
    AuditLog.patient_prev_hash,
)


def _chain_entry(row: Any) -> dict[str, Any]:
    """Row rendered as the dict shape :func:`verify_chain_from` consumes.

    ``row`` is an ``AuditLog`` or a ``Row`` over ``_CHAIN_COLUMNS`` — see :func:`_canonical_for`
    for why there is one of these rather than one per row shape.
    """
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
        "patient_prev_hash": row.patient_prev_hash,
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

    async def _patient_tails(self, patient_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
        """The last ``record_hash`` written for each of these patients, for the per-patient link.

        One indexed lookup per *distinct* patient in the batch, not per entry — a run that
        appends nine entries about one chart costs one. The read walks
        ``ix_audit_logs_patient_sequence`` backwards to its first row, so it does not degrade
        with the size of a table that is never pruned.

        A patient with no entries yet is absent from the result; :meth:`record_many` starts
        their chain at ``GENESIS_HASH``. That is deliberately not the same as ``None``, which
        means "this row predates the per-patient chain" — a first entry that recorded no link
        at all would be indistinguishable from a legacy row, and deleting the true first
        entries of a trail would then go unnoticed.
        """
        tails: dict[uuid.UUID, str] = {}
        for patient_id in patient_ids:
            tails[patient_id] = (
                await self.db.scalar(
                    select(AuditLog.record_hash)
                    .where(AuditLog.patient_id == patient_id)
                    .order_by(AuditLog.sequence.desc())
                    .limit(1)
                )
                or GENESIS_HASH
            )
        return tails

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

        patient_ids = {d.patient_id for d in drafts if d.patient_id is not None}

        for attempt in range(_MAX_SEQUENCE_ATTEMPTS):
            latest = await self._latest()
            sequence = (latest.sequence + 1) if latest else 1
            prev_hash = latest.record_hash if latest else GENESIS_HASH
            # Re-read inside the retry loop for the same reason the sequence is: a collision
            # means someone else appended, and what they appended may have been about one of
            # these patients.
            patient_prev = await self._patient_tails(patient_ids)

            entries: list[AuditLog] = []
            for offset, draft in enumerate(drafts):
                created_at = datetime.now(UTC)
                # Entries with no patient — the auth trail — take no per-patient link. There is
                # no chart-scoped walk that would ever check one, and the global chain already
                # covers them.
                patient_prev_hash = (
                    patient_prev[draft.patient_id] if draft.patient_id is not None else None
                )
                canonical = canonical_payload(
                    sequence=sequence + offset,
                    action=draft.action,
                    account_id=str(draft.account_id) if draft.account_id else None,
                    patient_id=str(draft.patient_id) if draft.patient_id else None,
                    entity_type=draft.entity_type,
                    entity_id=str(draft.entity_id) if draft.entity_id else None,
                    payload=draft.payload,
                    created_at=_iso_utc(created_at),
                    patient_prev_hash=patient_prev_hash,
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
                        patient_prev_hash=patient_prev_hash,
                        created_at=created_at,
                    )
                )
                # The next entry chains to this one, exactly as it would have done had it been
                # appended in its own call straight after this one — on both chains. A batch
                # carrying several entries about the same chart links them to each other, so
                # removing one from the middle of a run is as visible as removing one written
                # in its own call.
                prev_hash = record_hash
                if draft.patient_id is not None:
                    patient_prev[draft.patient_id] = record_hash

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

        # Structurally unreachable, and kept anyway: every iteration returns, re-raises on the
        # final attempt, or continues, so no input gets here. It exists so that a future edit
        # which does let the loop fall through fails loudly rather than returning None into a
        # caller that iterates it. Excluded from coverage because the only "test" for it would
        # be a lie about what the loop can do.
        raise AssertionError(  # pragma: no cover
            "unreachable: the loop returns or raises on the final attempt"
        )

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
        """Recompute the hash chain for a patient's entries, in batches, in sequence order.

        Two checks, because they catch different tampering. Each row's own ``record_hash`` is
        recomputed from what it stores, which catches an *edited* row. And each row's
        ``patient_prev_hash`` is checked against the previous row for the same patient, which
        catches a *deleted* one.

        The second check is the one this method used to be missing, and its absence made the
        endpoint's own promise false. ``prev_hash`` links a row to whatever was appended before
        it anywhere in the system, so a walk restricted to one chart had no linkage available:
        it recomputed surviving rows, all of which passed, and a removed entry left nothing
        behind to fail. That is the tamper worth doing — an entry recording that someone opened
        or exported a chart is deleted, not rewritten — and ``chain_valid: true`` came back.
        Migration 0025 added the per-patient link that makes the check possible; see
        :func:`~app.core.audit_hash.verify_patient_chain_from`.

        Rows written before that migration carry no link and are checked only on their own
        hash, which is what they were written under. The trail joins up at the boundary: the
        first linked row points at its legacy predecessor.

        This used to read the patient's whole trail in one statement, hydrate every row into an
        ORM instance, and hash the lot in a single expression. ``audit_logs`` is append-only and
        never pruned, and this table records every PHI read as well as every write — so the trail
        of a patient under long-term follow-up is tens of thousands of rows, and all of it landed
        in memory at once and was then hashed with the event loop held for the whole pass. That
        is the same shape of defect as the bcrypt and blob-I/O off-loads: unbounded work on the
        one thread this worker serves everybody from.

        Batching by a ``sequence`` cursor fixes both halves. Peak resident rows are one batch
        rather than one patient's history, and the ``await`` between batches hands the loop back,
        so hashing a long trail interleaves with everyone else's requests instead of freezing
        them. The cursor walks ``ix_audit_logs_patient_sequence`` forwards, so each batch is an
        index range scan rather than an offset that re-walks what has already been read.

        Columns rather than entities, for the same reason :meth:`list_for_patient` selects
        narrowly: nothing here needs a mapped object, and each one costs an instrumented instance
        and an identity-map entry per row for a value that is read once and thrown away.

        Every row is still counted and checked — no early exit on the first bad hash. The count
        is what the endpoint reports as ``entries_checked``, and tamper evidence that stops at
        the first discrepancy tells an operator less than one that covers the whole trail.
        """
        checked = 0
        valid = True
        after = -1
        expected_prev: str | None = None
        while True:
            result = await self.db.execute(
                select(*_CHAIN_COLUMNS)
                .where(AuditLog.patient_id == patient_id, AuditLog.sequence > after)
                .order_by(AuditLog.sequence.asc())
                .limit(_VERIFY_BATCH_SIZE)
            )
            rows = result.all()
            if not rows:
                return checked, valid
            checked += len(rows)
            after = rows[-1].sequence
            for row in rows:
                if compute_record_hash(row.prev_hash, _canonical_for(row)) != row.record_hash:
                    valid = False
            # Carried across the batch boundary, so a row deleted at the seam is as visible as
            # one deleted in the middle of a batch.
            links_valid, expected_prev = verify_patient_chain_from(
                [_chain_entry(r) for r in rows], expected_prev
            )
            valid = valid and links_valid

    async def verify_full_chain(self) -> tuple[int, bool]:
        """Verify the entire global chain is unbroken (genesis -> latest), in batches.

        Same defect and same fix as :meth:`verify_patient_chain`, one scale worse: this walks
        *every* row in ``audit_logs``, for every patient and every account, and it is reachable
        from a live endpoint — ``/regulatory/samd-dossier`` includes the chain's length and
        validity. Read in one statement, that put the whole never-pruned table in memory and
        hashed it with the event loop held.

        Linkage is what makes this the global check rather than the per-patient one, and linkage
        is exactly what a naive batch loses: each entry's ``prev_hash`` has to match the previous
        row's ``record_hash``, across a boundary as much as within one. ``verify_chain_from``
        carries that single expected hash from batch to batch, so the walk needs one row's worth
        of state rather than the table's.
        """
        checked = 0
        valid = True
        expected_prev = GENESIS_HASH
        after = -1
        while True:
            result = await self.db.execute(
                select(*_CHAIN_COLUMNS)
                .where(AuditLog.sequence > after)
                .order_by(AuditLog.sequence.asc())
                .limit(_VERIFY_BATCH_SIZE)
            )
            rows = result.all()
            if not rows:
                return checked, valid
            checked += len(rows)
            after = rows[-1].sequence
            batch_valid, expected_prev = verify_chain_from(
                [_chain_entry(r) for r in rows], expected_prev
            )
            valid = valid and batch_valid
