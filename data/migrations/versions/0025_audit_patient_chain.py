"""Add audit_logs.patient_prev_hash — a per-patient link in the tamper-evidence chain.

``GET /patients/{id}/audit/verify`` says that ``chain_valid: false`` means an entry was
"altered or removed underneath the application". It could only ever deliver the first half.
``prev_hash`` chains a row to whatever was appended before it *anywhere* in the system, so a
verification restricted to one patient has no linkage to check — it recomputes each surviving
row's own hash, and a row that has been deleted has no hash left to fail. Deletion is the
tamper this trail exists to catch: nobody edits the entry recording that they exported a chart,
they remove it, and the endpoint reported the chart clean.

This column is a second chain running through one patient's rows only, so the patient-scoped
walk can check linkage at the cost it already pays (an index range scan of
ix_audit_logs_patient_sequence) rather than by reading the whole table.

Nullable, and left NULL on every existing row. ``canonical_payload`` omits the field entirely
when it is NULL, so historical rows hash exactly as they did before this migration and keep
verifying. Backfilling it instead would mean rewriting the hashed representation of every row
in an append-only, tamper-evident table — which is the operation the table exists to make
detectable, so it is not one this migration performs.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.db.migration_guards import column_exists

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A database created today comes out of 0001 already carrying this column — 0001 builds the
    # schema from the live ORM models — so a bare ADD COLUMN would abort the upgrade here on
    # every fresh deployment. See app.db.migration_guards.
    bind = op.get_bind()
    if not column_exists(bind, "audit_logs", "patient_prev_hash"):
        op.add_column(
            "audit_logs", sa.Column("patient_prev_hash", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    op.drop_column("audit_logs", "patient_prev_hash")
