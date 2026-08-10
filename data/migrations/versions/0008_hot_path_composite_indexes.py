"""Composite indexes for the patient-scoped list queries that sort.

Every one of these paths already filters on an indexed foreign key, so none of them is a
sequential scan -- but each then sorts the matched rows in memory on a column the index
doesn't carry. The filtered sets grow without bound over a patient's history (labs and
medications accumulate per document ingested; audit_logs gains a row for every action in the
system and is never pruned), so the sort is the part that degrades, not the lookup.

Ordering each index to match the query's ORDER BY lets PostgreSQL read the rows out in order
and, where there is a LIMIT, stop early instead of sorting the whole set to return 25 rows.

Ordered by how much each is expected to matter:

* ``audit_logs (patient_id, sequence DESC)`` -- append-only compliance log, the largest table
  in the system over time, read with LIMIT/OFFSET by the audit view.
* ``patients (account_id, updated_at DESC) WHERE NOT is_deleted`` -- the most-hit endpoint.
  Partial on the soft-delete flag so the index holds only live rows.
* ``lab_results (patient_id, sample_date DESC NULLS LAST, marker_name)`` and
  ``medication_events (patient_id, is_current DESC, event_date DESC)`` -- both sorted on every
  longitudinal-record build, which is the read behind most clinical screens.
* ``documents (patient_id, created_at DESC)`` and
  ``clinical_suggestions (session_id, created_at)`` -- smaller per-parent sets, but the
  indexes are cheap and these are read on every document list / reasoning result.

Created CONCURRENTLY: these run against tables holding live patient data, and a plain CREATE
INDEX takes an ACCESS EXCLUSIVE-blocking write lock for its duration. Concurrent index builds
cannot run inside a transaction, hence the autocommit block.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-10
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, column list / index definition tail)
_INDEXES: list[tuple[str, str, str]] = [
    ("ix_audit_logs_patient_sequence", "audit_logs", "(patient_id, sequence DESC)"),
    (
        "ix_patients_account_updated_live",
        "patients",
        "(account_id, updated_at DESC) WHERE is_deleted = false",
    ),
    (
        "ix_lab_results_patient_sample_date",
        "lab_results",
        "(patient_id, sample_date DESC NULLS LAST, marker_name)",
    ),
    (
        "ix_medication_events_patient_current_date",
        "medication_events",
        "(patient_id, is_current DESC, event_date DESC)",
    ),
    ("ix_documents_patient_created", "documents", "(patient_id, created_at DESC)"),
    (
        "ix_clinical_suggestions_session_created",
        "clinical_suggestions",
        "(session_id, created_at)",
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        for name, table, definition in _INDEXES:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {definition}")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        for name, _table, _definition in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
