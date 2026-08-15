"""A deleted entry in a chart's audit trail used to verify clean.

``GET /patients/{id}/audit/verify`` says ``chain_valid: false`` means an entry was "altered or
removed underneath the application". It could only ever deliver the first half.

``prev_hash`` links a row to whatever was appended before it *anywhere in the system*, so a
verification restricted to one patient had no linkage available to check: it recomputed every
surviving row's own hash, all of which passed, and a removed row left nothing behind to fail.

That is the tamper worth doing. Nobody rewrites the entry recording that they opened or
exported a chart — the hash would stop matching and that check *did* work. They delete it, and
the endpoint reported the chart clean.

Migration 0025 adds ``patient_prev_hash``: a second chain running through one patient's rows
only, so a chart-scoped walk can check linkage at the cost it already pays. These tests hold
that in both directions — an untouched trail still verifies, an emptied slot does not — and
pin the two properties that make the column trustworthy rather than decorative: it is covered
by the record hash, so it cannot be quietly retied; and it is *absent* from the hashed body
when NULL, so every row written before the migration still verifies byte-for-byte.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select, update

from app.core.audit_hash import GENESIS_HASH, canonical_payload
from app.models.audit_log import AuditLog
from app.services.audit_service import AuditDraft, AuditService

PREFIX = "/api/v1"


async def _trail(db, patient_id: uuid.UUID) -> list[AuditLog]:
    return list(
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.patient_id == patient_id)
                .order_by(AuditLog.sequence.asc())
            )
        )
        .scalars()
        .all()
    )


async def _charted(db, patient_id: uuid.UUID, count: int = 5) -> None:
    """A trail for one patient, with another patient's entries interleaved between them.

    The interleaving is the point: it is why the global ``prev_hash`` cannot be used to check
    this patient's slice, and so why the per-patient link had to exist.
    """
    other = uuid.uuid4()
    audit = AuditService(db)
    for index in range(count):
        await audit.record(action="patient_viewed", patient_id=patient_id, payload={"i": index})
        await audit.record(action="patient_viewed", patient_id=other, payload={"i": index})
    await db.commit()


@pytest.fixture
def patient_id() -> uuid.UUID:
    return uuid.uuid4()


# --- The property that was missing --------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_untouched_trail_verifies(db, patient_id):
    await _charted(db, patient_id)
    checked, valid = await AuditService(db).verify_patient_chain(patient_id)
    assert (checked, valid) == (5, True)


@pytest.mark.asyncio
async def test_deleting_an_entry_from_the_middle_of_a_trail_is_detected(db, patient_id):
    """The regression this whole change exists for. Before the per-patient link, every
    surviving row still recomputed correctly and this returned ``True``."""
    await _charted(db, patient_id)
    removed = (await _trail(db, patient_id))[2]

    await db.execute(delete(AuditLog).where(AuditLog.id == removed.id))
    await db.commit()

    checked, valid = await AuditService(db).verify_patient_chain(patient_id)
    assert checked == 4
    assert valid is False


@pytest.mark.asyncio
async def test_deleting_the_first_entry_of_a_trail_is_detected(db, patient_id):
    """A chart's first entry is the one saying it was created, and it chains from genesis — so
    removing it orphans its successor rather than leaving a trail that starts a little later."""
    await _charted(db, patient_id)
    first = (await _trail(db, patient_id))[0]

    await db.execute(delete(AuditLog).where(AuditLog.id == first.id))
    await db.commit()

    _, valid = await AuditService(db).verify_patient_chain(patient_id)
    assert valid is False


@pytest.mark.asyncio
async def test_deleting_a_run_of_entries_is_detected(db, patient_id):
    """Removing the neighbours does not repair the link: the survivor still points at a hash
    that is no longer the one before it."""
    await _charted(db, patient_id, count=6)
    trail = await _trail(db, patient_id)

    await db.execute(delete(AuditLog).where(AuditLog.id.in_([trail[1].id, trail[2].id])))
    await db.commit()

    _, valid = await AuditService(db).verify_patient_chain(patient_id)
    assert valid is False


@pytest.mark.asyncio
async def test_deleting_another_patients_entry_does_not_implicate_this_chart(db, patient_id):
    """The chart-scoped answer stays about this chart. A trail reported as tampered because
    something happened on a different patient is a check nobody can act on."""
    other = uuid.uuid4()
    audit = AuditService(db)
    await audit.record(action="patient_viewed", patient_id=patient_id)
    await audit.record(action="patient_viewed", patient_id=other)
    await audit.record(action="patient_viewed", patient_id=patient_id)
    await db.commit()

    victim = (await _trail(db, other))[0]
    await db.execute(delete(AuditLog).where(AuditLog.id == victim.id))
    await db.commit()

    _, valid = await AuditService(db).verify_patient_chain(patient_id)
    assert valid is True


@pytest.mark.asyncio
async def test_an_edited_entry_is_still_detected(db, patient_id):
    """The check that already worked, kept working. The new link is in addition to
    recomputation, not instead of it."""
    await _charted(db, patient_id)
    target = (await _trail(db, patient_id))[1]

    await db.execute(
        update(AuditLog).where(AuditLog.id == target.id).values(action="patient_created")
    )
    await db.commit()

    _, valid = await AuditService(db).verify_patient_chain(patient_id)
    assert valid is False


@pytest.mark.asyncio
async def test_the_link_cannot_be_retied_to_cover_a_deletion(db, patient_id):
    """The link is inside the hash, so an editor who removes an entry and repairs its
    successor's ``patient_prev_hash`` breaks that successor's own ``record_hash`` instead.
    Without this the column would be decorative — a pointer anyone could rewrite."""
    await _charted(db, patient_id)
    trail = await _trail(db, patient_id)

    await db.execute(delete(AuditLog).where(AuditLog.id == trail[2].id))
    await db.execute(
        update(AuditLog)
        .where(AuditLog.id == trail[3].id)
        .values(patient_prev_hash=trail[1].record_hash)
    )
    await db.commit()

    _, valid = await AuditService(db).verify_patient_chain(patient_id)
    assert valid is False


# --- How it is written --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_patients_first_entry_chains_from_genesis_not_from_nothing(db, patient_id):
    """A first entry that recorded no link at all would be indistinguishable from a row written
    before the migration — so deleting the true first entries of a trail would be invisible."""
    await AuditService(db).record(action="patient_created", patient_id=patient_id)
    await db.commit()

    assert (await _trail(db, patient_id))[0].patient_prev_hash == GENESIS_HASH


@pytest.mark.asyncio
async def test_entries_appended_in_one_batch_link_to_each_other(db, patient_id):
    """A reasoning run hands over its closing entries together. Removing one from the middle of
    that batch has to be as visible as removing one written in its own call."""
    trail_before = await AuditService(db).record_many(
        [
            AuditDraft(action="reasoning_session_completed", patient_id=patient_id),
            AuditDraft(action="hard_block_triggered", patient_id=patient_id),
            AuditDraft(action="clinical_suggestion_created", patient_id=patient_id),
        ]
    )
    await db.commit()

    assert trail_before[0].patient_prev_hash == GENESIS_HASH
    assert trail_before[1].patient_prev_hash == trail_before[0].record_hash
    assert trail_before[2].patient_prev_hash == trail_before[1].record_hash

    await db.execute(delete(AuditLog).where(AuditLog.id == trail_before[1].id))
    await db.commit()
    _, valid = await AuditService(db).verify_patient_chain(patient_id)
    assert valid is False


@pytest.mark.asyncio
async def test_an_entry_with_no_patient_takes_no_per_patient_link(db):
    """The auth trail has no chart to chain through, and no chart-scoped walk would ever check
    one. The global chain already covers those rows."""
    entry = await AuditService(db).record(action="auth_login_success")
    await db.commit()

    assert entry.patient_prev_hash is None


@pytest.mark.asyncio
async def test_the_chart_scoped_link_does_not_disturb_the_global_chain(db, patient_id):
    """Both chains are live at once and neither is allowed to weaken the other."""
    await _charted(db, patient_id)
    checked, valid = await AuditService(db).verify_full_chain()
    assert valid is True
    assert checked >= 10


# --- Rows written before the migration ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_legacy_entry_hashes_exactly_as_it_did_before_the_column_existed(db, patient_id):
    """``canonical_payload`` omits the field when it is NULL rather than serialising ``null``.

    If it did not, the first verification after deploying migration 0025 would report the whole
    historical trail as tampered — tamper evidence for a deploy, which is exactly the false
    positive that teaches an operator to stop reading it.
    """

    def canonical(link: str | None) -> str:
        return canonical_payload(
            sequence=1,
            action="patient_viewed",
            account_id=None,
            patient_id=str(patient_id),
            entity_type="patient",
            entity_id=str(patient_id),
            payload={},
            created_at="2026-08-15T00:00:00+00:00",
            patient_prev_hash=link,
        )

    assert "patient_prev_hash" not in canonical(None)
    # And when it is set, it *is* covered — otherwise the link would be forgeable.
    assert canonical(GENESIS_HASH) != canonical(None)
    assert "patient_prev_hash" in canonical(GENESIS_HASH)


@pytest.mark.asyncio
async def test_a_trail_written_before_the_migration_still_verifies(db, patient_id):
    """Existing rows carry no link. They are checked on their own hash, which is what they were
    written under, rather than failed for missing something that did not exist yet."""
    await _charted(db, patient_id, count=3)
    await db.execute(
        update(AuditLog).where(AuditLog.patient_id == patient_id).values(patient_prev_hash=None)
    )
    await db.commit()

    # Recompute what those rows would have hashed to without the field, as the pre-migration
    # writer did — the fixture wrote them *with* a link, so their stored hash covers it.
    for row in await _trail(db, patient_id):
        row.record_hash = _legacy_hash(row)
    await db.commit()

    _, valid = await AuditService(db).verify_patient_chain(patient_id)
    assert valid is True


@pytest.mark.asyncio
async def test_the_two_eras_join_up_at_the_boundary(db, patient_id):
    """A patient's first linked entry points at its legacy predecessor, so the chain does not
    silently restart at the migration and leave the rows either side of it uncheckable."""
    await _charted(db, patient_id, count=2)
    for row in await _trail(db, patient_id):
        row.patient_prev_hash = None
        row.record_hash = _legacy_hash(row)
    await db.commit()

    legacy_tail = (await _trail(db, patient_id))[-1]
    await AuditService(db).record(action="patient_record_exported", patient_id=patient_id)
    await db.commit()

    trail = await _trail(db, patient_id)
    assert trail[-1].patient_prev_hash == legacy_tail.record_hash
    _, valid = await AuditService(db).verify_patient_chain(patient_id)
    assert valid is True

    # And a legacy row removed from under that boundary is now caught, because the first linked
    # entry no longer points at the row that ends up before it.
    await db.execute(delete(AuditLog).where(AuditLog.id == legacy_tail.id))
    await db.commit()
    _, valid = await AuditService(db).verify_patient_chain(patient_id)
    assert valid is False


def _legacy_hash(row: AuditLog) -> str:
    """The ``record_hash`` this row would carry had it been written before migration 0025."""
    from app.services.audit_service import _canonical_for, compute_record_hash

    row.patient_prev_hash = None
    return compute_record_hash(row.prev_hash, _canonical_for(row))
