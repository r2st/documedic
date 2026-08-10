"""Drop indexes that 0008's composites (and one that nothing) made redundant.

0008 added six composite indexes but left the single-column indexes they subsume in place.
A B-tree on ``(a, b)`` answers every lookup a B-tree on ``(a)`` can -- same leading column,
same ordering -- so each of those single-column indexes is now dead weight that still has to
be maintained on every INSERT, UPDATE and DELETE of its table. That cost lands hardest on
``audit_logs``, which takes a write for every action in the system and is never pruned.

Verified on a PostgreSQL 16 instance seeded to 774k audit rows / 106k labs / 50k patients: all
nine were dropped inside a transaction, every read path re-EXPLAINed, and the transaction
rolled back. Each query kept its plan *shape* -- the planner substituted the composite for the
dropped index in the same node, and no plan degraded to a sequential scan.

Prefix-redundant (dropped index is a leading-column prefix of a surviving one):

===========================================  ==========================================
dropped                                      subsumed by
===========================================  ==========================================
ix_audit_logs_patient_id                     ix_audit_logs_patient_sequence
ix_patients_account_id                       ix_patients_account_updated_live
ix_lab_results_patient_id                    ix_lab_results_patient_sample_date
ix_medication_events_patient_id              ix_medication_events_patient_current_date
ix_documents_patient_id                      ix_documents_patient_created
ix_clinical_suggestions_session_id           ix_clinical_suggestions_session_created
ix_guideline_chunks_corpus_version           uq_guideline_chunks_section
ix_drug_interactions_drug_a_reference_id     uq_drug_interactions_pair
===========================================  ==========================================

``ix_patients_account_updated_live`` is *partial* (``WHERE is_deleted = false``), so it only
subsumes queries that carry that predicate. Every ``Patient.account_id`` filter in the codebase
does -- patient_service.get/list, safety_service._patient, lab_safety_service._patient,
reasoning_service._patient -- and each was checked individually before this drop.

Also dropped, not redundant but unused: ``ix_audit_logs_record_hash``. ``record_hash`` is
never a lookup key -- chain verification recomputes each hash from the row it already has
(app/core/audit_hash.py) rather than searching by it -- so the index has never served a scan.
It is also the most expensive one here: 56 MB at 774k rows, on the table with the highest
write rate and no retention policy. The column stays; only the index goes.

One measured regression, accepted deliberately. The audit page's unpaginated COUNT for a
patient with 20k entries goes from 1.08 ms / 19 buffers (index-only scan of the small
``ix_audit_logs_patient_id``) to 1.32 ms / 180 buffers (index-only scan of the wider
``ix_audit_logs_patient_sequence``, which also carries ``sequence``). That is +0.24 ms once
per audit page load, traded against one fewer B-tree insert on every audited action.

Reclaimed at the seeded size: ~65 MB, of which ~62 MB is on ``audit_logs``. The saving grows
with row count; the write saving is per-row and permanent.

DROP INDEX CONCURRENTLY, like CREATE INDEX CONCURRENTLY in 0008, takes no
ACCESS EXCLUSIVE lock and cannot run inside a transaction -- hence the autocommit block.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-10
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, definition tail) -- the tail is what downgrade() recreates.
_REDUNDANT: list[tuple[str, str, str]] = [
    ("ix_audit_logs_patient_id", "audit_logs", "(patient_id)"),
    ("ix_patients_account_id", "patients", "(account_id)"),
    ("ix_lab_results_patient_id", "lab_results", "(patient_id)"),
    ("ix_medication_events_patient_id", "medication_events", "(patient_id)"),
    ("ix_documents_patient_id", "documents", "(patient_id)"),
    ("ix_clinical_suggestions_session_id", "clinical_suggestions", "(session_id)"),
    ("ix_guideline_chunks_corpus_version", "guideline_chunks", "(corpus_version)"),
    ("ix_drug_interactions_drug_a_reference_id", "drug_interactions", "(drug_a_reference_id)"),
    ("ix_audit_logs_record_hash", "audit_logs", "(record_hash)"),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # DROP INDEX CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        for name, _table, _definition in _REDUNDANT:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        for name, table, definition in _REDUNDANT:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {definition}")
