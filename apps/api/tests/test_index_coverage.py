"""Guards for the index set: no redundancy, no gaps, and the reads still return the right rows.

Migration 0009 removed nine indexes that 0008's composites had made redundant (plus one that
never served a scan). Three things can silently undo that work, so each gets a test:

* Someone re-adds ``index=True`` to a column already led by a composite. ``test_no_index_is_a
  _prefix_of_another`` fails on any leading-column prefix pair, not just the nine known ones.
* The migration and the ORM models drift apart -- ``Base.metadata.create_all()`` builds a fresh
  deploy's schema (migration 0001), so an index present in the models but dropped by 0009 would
  exist on new databases and not on upgraded ones. ``test_dropped_indexes_are_absent_from
  _models`` pins them together.
* An index is dropped that was actually load-bearing. The behavioural tests below exercise each
  affected read path and assert on ordering and contents, which is what the index has to keep
  correct; the query *plans* were verified separately against a seeded PostgreSQL 16 instance
  (see the 0009 docstring) and cannot be asserted here, since the suite runs on SQLite.
"""

from __future__ import annotations

import ast
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, select

from app.models import Base
from app.models.audit_log import AuditLog
from app.models.document import Document
from app.models.guideline import GuidelineChunk
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.audit_service import AuditService
from tests.conftest import create_patient

MIGRATION_0009 = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "migrations"
    / "versions"
    / "0009_drop_redundant_indexes.py"
)


def _dropped_index_names() -> set[str]:
    """The index names migration 0009 drops, read out of its ``_REDUNDANT`` table."""
    tree = ast.parse(MIGRATION_0009.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign | ast.Assign) and isinstance(
            getattr(node, "value", None), ast.List
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "_REDUNDANT" for t in targets):
                return {
                    entry.elts[0].value
                    for entry in node.value.elts
                    if isinstance(entry, ast.Tuple) and isinstance(entry.elts[0], ast.Constant)
                }
    raise AssertionError(f"no _REDUNDANT list found in {MIGRATION_0009.name}")


def _leading_columns(index) -> tuple[str, ...]:
    """Column names of an index in order, with non-column expressions rendered as SQL text.

    ``Index("...", "patient_id", text("sequence DESC"))`` mixes bound columns and raw text; both
    have to be comparable for the prefix check to mean anything.
    """
    names: list[str] = []
    for expr in index.expressions:
        name = getattr(expr, "name", None)
        names.append(name if isinstance(name, str) else str(expr).split()[0])
    return tuple(names)


# --------------------------------------------------------------------------- structural


def test_migration_0009_lists_the_expected_indexes():
    """Guard the guard: a parse failure would make the checks below vacuously pass."""
    dropped = _dropped_index_names()
    assert len(dropped) == 9, dropped
    assert "ix_audit_logs_patient_id" in dropped
    assert "ix_audit_logs_record_hash" in dropped


def test_dropped_indexes_are_absent_from_models():
    """0009 and the ORM must agree, or fresh deploys and upgraded ones diverge.

    Migration 0001 builds a new database from ``Base.metadata``. Any index still declared there
    but dropped by 0009 would be created on a fresh deploy and absent on an upgraded one -- the
    two would then have different schemas under the same revision.
    """
    declared = {index.name for table in Base.metadata.tables.values() for index in table.indexes}
    still_declared = declared & _dropped_index_names()
    assert not still_declared, (
        f"migration 0009 drops {sorted(still_declared)} but the models still declare them; a "
        "fresh deploy (create_all) would have indexes an upgraded database does not"
    )


def test_no_index_is_a_prefix_of_another():
    """No index may be a strict leading-column prefix of another on the same table.

    A B-tree on ``(a, b)`` answers everything a B-tree on ``(a)`` answers, so the shorter one
    only costs writes. Partial indexes are exempt as *subsumers*: they cover only the rows
    matching their predicate, so they cannot stand in for an unconditional index.
    """
    offenders: list[str] = []
    for table in Base.metadata.tables.values():
        indexes = list(table.indexes)
        for short in indexes:
            for long in indexes:
                if short is long or len(long.expressions) <= len(short.expressions):
                    continue
                if long.dialect_options.get("postgresql", {}).get("where") is not None:
                    continue  # partial: covers a subset of rows, subsumes nothing
                if _leading_columns(long)[: len(short.expressions)] == _leading_columns(short):
                    offenders.append(f"{table.name}.{short.name} is a prefix of {long.name}")
    assert not offenders, (
        "redundant indexes (each costs a write on every row change and buys nothing): "
        + "; ".join(sorted(offenders))
    )


@pytest.mark.parametrize(
    ("table_name", "expected_leading"),
    [
        ("audit_logs", "patient_id"),
        ("patients", "account_id"),
        ("lab_results", "patient_id"),
        ("medication_events", "patient_id"),
        ("documents", "patient_id"),
        ("clinical_suggestions", "session_id"),
    ],
)
def test_scoping_column_still_leads_some_index(table_name: str, expected_leading: str):
    """Every column whose single-column index 0009 dropped is still led by a surviving index.

    This is the half of the change that could actually hurt: dropping the plain index is only
    safe while a composite keeps that column in the leading position. ``lab_results`` is the
    subtle one -- its PostgreSQL composite carries ``NULLS LAST``, which SQLite's parser
    rejects, so a dialect-specific sibling exists purely so the test/dev database is not left
    with ``patient_id`` unindexed.

    ``guideline_chunks`` and ``drug_interactions`` are absent here on purpose: their subsumers
    are UNIQUE constraints rather than Index objects, so they are checked separately below.
    """
    table = Base.metadata.tables[table_name]
    leaders = {_leading_columns(index)[0] for index in table.indexes if index.expressions}
    assert expected_leading in leaders, (
        f"{table_name}.{expected_leading} leads no index; migration 0009 dropped its "
        f"single-column index on the assumption a composite still leads with it. Present: "
        f"{sorted(leaders)}"
    )


def test_unique_constraints_still_cover_the_dropped_prefixes():
    """The two non-composite subsumers are UNIQUE constraints, not Index objects.

    ``uq_guideline_chunks_section`` and ``uq_drug_interactions_pair`` back the drops of
    ``ix_guideline_chunks_corpus_version`` and ``ix_drug_interactions_drug_a_reference_id``.
    They live in ``table.constraints``, so the Index-based checks above cannot see them.
    """
    chunks = Base.metadata.tables["guideline_chunks"]
    interactions = Base.metadata.tables["drug_interactions"]

    def _unique_leading(table, name: str) -> str:
        for constraint in table.constraints:
            if constraint.name == name:
                return list(constraint.columns)[0].name
        raise AssertionError(f"{table.name} has no unique constraint {name}")

    assert _unique_leading(chunks, "uq_guideline_chunks_section") == "corpus_version"
    assert _unique_leading(interactions, "uq_drug_interactions_pair") == "drug_a_reference_id"


# --------------------------------------------------------------------------- behavioural


async def _account_with_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"idx-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Index Coverage Patient",
        sex="male",
        date_of_birth=date(1970, 1, 1),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def test_patient_list_is_account_scoped_and_newest_first(auth_client):
    """The patient list read that ix_patients_account_updated_live serves, end to end.

    Its subsuming index is partial on ``is_deleted = false``, so a soft-deleted patient has no
    index entry at all -- this asserts one does not come back, which is the failure mode that
    would matter if the predicate and the query ever disagreed.
    """
    first = await create_patient(auth_client, full_name="Aarti First")
    second = await create_patient(auth_client, full_name="Bhavna Second")
    third = await create_patient(auth_client, full_name="Chetan Third")

    resp = await auth_client.delete(f"/api/v1/patients/{second['id']}")
    assert resp.status_code in (200, 204), resp.text

    resp = await auth_client.get("/api/v1/patients?limit=25&offset=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    returned = [p["id"] for p in body["items"]]

    assert second["id"] not in returned, "soft-deleted patient leaked into the list"
    assert set(returned) == {first["id"], third["id"]}
    assert body["pagination"]["total"] == 2
    # Ordering is asserted as monotonicity rather than a fixed id order: `updated_at` defaults
    # to the server's CURRENT_TIMESTAMP, which is second-resolution on SQLite, so patients
    # created within the same second legitimately tie. test_patient_list_orders_by_updated_at
    # pins the direction with timestamps it controls.
    timestamps = [p["updated_at"] for p in body["items"]]
    assert timestamps == sorted(timestamps, reverse=True), timestamps


async def test_patient_list_orders_by_updated_at_descending(db):
    """The DESC ordering ix_patients_account_updated_live carries, with unambiguous timestamps.

    Written through the service rather than the API so ``updated_at`` can be set explicitly --
    the column's server default has one-second resolution on SQLite, which is too coarse to
    order rows created by a test.
    """
    from app.services.patient_service import PatientService

    account, oldest = await _account_with_patient(db)
    middle = Patient(
        account_id=account.id,
        full_name="Middle Patient",
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    newest = Patient(
        account_id=account.id,
        full_name="Newest Patient",
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add_all([middle, newest])
    await db.flush()

    oldest.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    middle.updated_at = datetime(2026, 4, 1, tzinfo=UTC)
    newest.updated_at = datetime(2026, 8, 1, tzinfo=UTC)
    await db.commit()

    items, total = await PatientService(db).list(account.id, limit=25, offset=0)
    assert total == 3
    assert [p.full_name for p in items] == [
        "Newest Patient",
        "Middle Patient",
        "Index Coverage Patient",
    ]


async def test_lab_results_sort_newest_first_with_undated_last(db):
    """The ORDER BY that ix_lab_results_patient_sample_date exists to serve.

    ``sample_date`` is nullable and the sort is NULLS LAST, which is exactly the part the
    PostgreSQL index spells out and the SQLite sibling cannot -- so the ordering itself is
    worth asserting on rather than trusting the index definition to imply it.
    """
    from app.services.record_service import RecordService

    _, patient = await _account_with_patient(db)
    rows = [
        LabResult(patient_id=patient.id, marker_name="HbA1c", sample_date=None),
        LabResult(
            patient_id=patient.id,
            marker_name="Creatinine",
            sample_date=datetime(2026, 1, 5, tzinfo=UTC),
        ),
        LabResult(
            patient_id=patient.id,
            marker_name="Potassium",
            sample_date=datetime(2026, 6, 1, tzinfo=UTC),
        ),
    ]
    db.add_all(rows)
    await db.commit()

    record = await RecordService(db).assemble(patient.id)
    ordered = [lab.marker_name for lab in record.lab_results]
    assert ordered == ["Potassium", "Creatinine", "HbA1c"], ordered


async def test_medication_events_sort_current_first_then_newest(db):
    """The ORDER BY that ix_medication_events_patient_current_date exists to serve."""
    from app.services.record_service import RecordService

    _, patient = await _account_with_patient(db)
    db.add_all(
        [
            MedicationEvent(
                patient_id=patient.id,
                generic_name="Old Stopped",
                event_type="stop",
                is_current=False,
                event_date=date(2026, 7, 1),
            ),
            MedicationEvent(
                patient_id=patient.id,
                generic_name="Older Current",
                event_type="start",
                is_current=True,
                event_date=date(2026, 1, 1),
            ),
            MedicationEvent(
                patient_id=patient.id,
                generic_name="Newer Current",
                event_type="start",
                is_current=True,
                event_date=date(2026, 5, 1),
            ),
        ]
    )
    await db.commit()

    record = await RecordService(db).assemble(patient.id)
    ordered = [med.generic_name for med in record.medications]
    # Current medications first regardless of date, then most recent within each group.
    assert ordered == ["Newer Current", "Older Current", "Old Stopped"], ordered


async def test_document_list_is_patient_scoped_and_newest_first(db):
    """The read ix_documents_patient_created serves; a second patient's rows must not appear."""
    from app.services.document_service import DocumentService

    account, patient = await _account_with_patient(db)
    other = Patient(
        account_id=account.id,
        full_name="Other Patient",
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(other)
    await db.flush()

    def _doc(owner: Patient, name: str) -> Document:
        return Document(
            patient_id=owner.id,
            account_id=account.id,
            file_name=name,
            file_type="pdf",
            file_size_bytes=1,
            storage_path=f"/tmp/{name}",
            storage_hash_sha256=uuid.uuid4().hex * 2,
        )

    db.add_all([_doc(patient, "first.pdf"), _doc(patient, "second.pdf"), _doc(other, "theirs.pdf")])
    await db.commit()

    documents = await DocumentService(db).list(account.id, patient.id)
    names = [d.file_name for d in documents]
    assert "theirs.pdf" not in names, "another patient's document leaked into the list"
    assert sorted(names) == ["first.pdf", "second.pdf"]


async def test_audit_page_is_patient_scoped_and_ordered_without_the_dropped_index(db):
    """The two-phase audit page still pages correctly with ix_audit_logs_patient_id gone.

    It was the index the unpaginated COUNT used; the count now runs off the wider composite.
    Both halves of the read -- the page and the total -- are asserted here.
    """
    account, patient = await _account_with_patient(db)
    other = Patient(
        account_id=account.id,
        full_name="Unrelated Patient",
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(other)
    await db.flush()

    service = AuditService(db)
    for index in range(7):
        await service.record(
            action="patient_viewed",
            account_id=account.id,
            patient_id=patient.id,
            entity_type="patient",
            entity_id=patient.id,
            payload={"i": index},
        )
    await service.record(
        action="patient_viewed",
        account_id=account.id,
        patient_id=other.id,
        entity_type="patient",
        entity_id=other.id,
    )
    await db.commit()

    page_one, total = await service.list_for_patient(patient.id, limit=3, offset=0)
    page_two, _ = await service.list_for_patient(patient.id, limit=3, offset=3)

    assert total == 7, "count must exclude the other patient's entry"
    assert [e.payload["i"] for e in page_one] == [6, 5, 4]
    assert [e.payload["i"] for e in page_two] == [3, 2, 1]
    assert all(e.patient_id == patient.id for e in page_one + page_two)


async def test_audit_chain_verification_still_passes_without_the_record_hash_index(db):
    """0009 drops ix_audit_logs_record_hash; tamper-evidence must be unaffected.

    Verification recomputes each hash from the row it already holds rather than looking one up
    by value, so removing the index cannot change the verdict -- this pins that it does not.
    """
    account, patient = await _account_with_patient(db)
    service = AuditService(db)
    for action in ("patient_created", "patient_viewed", "patient_updated"):
        await service.record(
            action=action,
            account_id=account.id,
            patient_id=patient.id,
            entity_type="patient",
            entity_id=patient.id,
        )
    await db.commit()

    count, valid = await service.verify_patient_chain(patient.id)
    assert (count, valid) == (3, True)

    _, globally_valid = await service.verify_full_chain()
    assert globally_valid is True


async def test_guideline_lookup_by_corpus_version_still_resolves(db):
    """ix_guideline_chunks_corpus_version is gone; uq_guideline_chunks_section leads with it."""
    from app.services.guideline_service import GuidelineService

    db.add_all(
        [
            GuidelineChunk(
                corpus_version="v-test-1",
                source="icmr",
                section_id="sec-a",
                document_title="Standard Treatment Workflow: Hypertension",
                content="Guidelines support considering lifestyle modification first.",
            ),
            GuidelineChunk(
                corpus_version="v-test-2",
                source="icmr",
                section_id="sec-a",
                document_title="Standard Treatment Workflow: Hypertension (revised)",
                content="Revised guidance.",
            ),
        ]
    )
    await db.commit()

    assert await GuidelineService(db).count(corpus_version="v-test-1") == 1
    assert await GuidelineService(db).count(corpus_version="v-test-2") == 1


async def test_drug_interaction_pair_uniqueness_survives(db):
    """The unique constraint that subsumes ix_drug_interactions_drug_a_reference_id still bites."""
    from sqlalchemy.exc import IntegrityError

    from app.models.drug_vocabulary import DrugInteraction

    db.add(
        DrugInteraction(
            drug_a_reference_id="ref-a",
            drug_b_reference_id="ref-b",
            severity="major",
            description="d",
            source="test-fixture",
        )
    )
    await db.commit()

    db.add(
        DrugInteraction(
            drug_a_reference_id="ref-a",
            drug_b_reference_id="ref-b",
            severity="major",
            description="duplicate",
            source="test-fixture",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    # Same leading drug, different partner: still allowed.
    db.add(
        DrugInteraction(
            drug_a_reference_id="ref-a",
            drug_b_reference_id="ref-c",
            severity="minor",
            description="different pair",
            source="test-fixture",
        )
    )
    await db.commit()
    result = await db.execute(
        select(DrugInteraction).where(DrugInteraction.drug_a_reference_id == "ref-a")
    )
    assert len(result.scalars().all()) == 2


async def test_audit_log_still_writes_and_reads_after_index_removal(db):
    """End-to-end smoke: an AuditLog row round-trips with neither dropped index present."""
    account, patient = await _account_with_patient(db)
    service = AuditService(db)
    entry = await service.record(
        action="document_downloaded",
        account_id=account.id,
        patient_id=patient.id,
        entity_type="document",
        entity_id=uuid.uuid4(),
    )
    await db.commit()

    fetched = (
        await db.execute(select(AuditLog).where(AuditLog.record_hash == entry.record_hash))
    ).scalar_one()
    assert fetched.id == entry.id
    assert fetched.patient_id == patient.id


# ------------------------------------------------------- sort-optimization claims (0008)


@contextmanager
def _captured_sql(engine):
    """Collect every SQL statement executed on `engine` for the duration of the block."""
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


async def test_only_the_paginated_reads_can_use_their_index_ordering(db, engine):
    """Pin which of 0008's six composites actually save their read a sort.

    An index's ordering only earns its keep when the query has a LIMIT: without one the planner
    must read the whole matched set regardless, and sequential heap access plus a quicksort
    beats fetching every row in index order. Measured on PostgreSQL 16 when 0008 landed, only
    two of its six composites served a read that could stop early; the other four bitmap-scanned
    the leading column and sorted.

    Two of those four have since been paginated -- the longitudinal record's labs and
    medication events -- which is what the composites' trailing columns were left in place for.
    The document list is the one still reading its whole set.

    That measurement cannot be reproduced here -- the suite runs on SQLite, which has a
    different planner -- but its *cause* can be. This asserts which reads carry a LIMIT, which
    is the property the comments in 0008 and the ORM models are written against. If someone
    paginates the document list, this fails and points at the comments that then become wrong.
    """
    from app.services.document_service import DocumentService
    from app.services.patient_service import PatientService
    from app.services.record_service import RecordService

    account, patient = await _account_with_patient(db)
    await db.commit()
    service = AuditService(db)
    await service.record(
        action="patient_viewed",
        account_id=account.id,
        patient_id=patient.id,
        entity_type="patient",
        entity_id=patient.id,
    )
    await db.commit()

    def _ordered_selects(statements: list[str]) -> list[str]:
        return [s for s in statements if "ORDER BY" in s.upper() and "SELECT" in s.upper()]

    # --- paginated: the ordering can stop early, so 0008's claim holds for these two.
    with _captured_sql(engine) as sql:
        await PatientService(db).list(account.id, limit=25, offset=0)
    patient_list = _ordered_selects(sql)
    assert patient_list, "expected an ordered SELECT from PatientService.list"
    assert all("LIMIT" in s.upper() for s in patient_list), patient_list

    with _captured_sql(engine) as sql:
        await service.list_for_patient(patient.id, limit=50, offset=0)
    audit_page = _ordered_selects(sql)
    assert audit_page, "expected an ordered SELECT from AuditService.list_for_patient"
    assert any("LIMIT" in s.upper() for s in audit_page), audit_page

    # --- unbounded: no LIMIT, so the trailing sort columns buy nothing today.
    with _captured_sql(engine) as sql:
        await DocumentService(db).list(account.id, patient.id)
    documents = _ordered_selects(sql)
    assert documents, "expected an ordered SELECT from DocumentService.list"
    assert not any("LIMIT" in s.upper() for s in documents), (
        "DocumentService.list is now paginated; ix_documents_patient_created can serve the "
        "ordering after all, so update the comments in 0008 and app/models/document.py"
    )

    # --- paginated as of the record-pagination change: assemble pages every section, so its
    # ordered reads carry a LIMIT and the lab_results / medication_events composites can stop
    # early on their ordering. If this ever reverts to whole-set reads, the comments in 0008
    # and in the two ORM models go back to describing the old measurement.
    with _captured_sql(engine) as sql:
        await RecordService(db).assemble(patient.id)
    record_reads = _ordered_selects(sql)
    assert len(record_reads) >= 2, record_reads
    assert all("LIMIT" in s.upper() for s in record_reads), (
        "RecordService.assemble stopped paginating one of its sections; the comments in 0008 "
        "and app/models/{lab_result,medication_event}.py describe a paginated read"
    )
    # The count that makes `total` truthful must not be paying for a sort it discards. Matched
    # on "COUNT(" rather than "COUNT": four of the five sections select an `encounter_id`, and
    # a bare substring match finds the "count" inside "encounter".
    counts = [s for s in sql if "COUNT(" in s.upper()]
    assert len(counts) == 5, counts
    assert not any("ORDER BY" in s.upper() for s in counts), counts
