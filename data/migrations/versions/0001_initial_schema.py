"""Initial Phase 1 schema: auth, patients, documents, patient graph, drug safety, audit log.

Tables are created from the SQLAlchemy metadata (single source of truth shared with the ORM),
then PostgreSQL-specific safeguards are layered on:
  * pgcrypto / pg_trgm extensions
  * fn_set_updated_at() + per-table BEFORE UPDATE triggers on mutable tables
  * an append-only guard on audit_logs (reject UPDATE/DELETE) — the Phase 1 audit trail is
    immutable, mirroring the clinical_suggestions design that arrives in Phase 2.

Revision ID: 0001
Revises:
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Make app models importable so we can reuse their metadata.
from app.models import Base  # noqa: E402

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mutable tables that get an auto-updating updated_at trigger (Postgres).
_MUTABLE_TABLES = [
    "accounts",
    "sessions",
    "patients",
    "documents",
    "encounters",
    "medication_events",
    "lab_results",
    "conditions",
    "allergies",
    "derived_markers",
    "drug_vocabulary",
    "drug_interactions",
    "contraindications",
]

_SET_UPDATED_AT_FN = """
CREATE OR REPLACE FUNCTION fn_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_AUDIT_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION fn_audit_logs_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs rows are immutable. UPDATE and DELETE are prohibited.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Create every Phase 1 table directly from the ORM metadata.
    Base.metadata.create_all(bind=bind)

    if not is_pg:
        return

    # One command per op.execute: the asyncpg driver runs DDL through a prepared statement,
    # which rejects multi-command strings ("cannot insert multiple commands into a prepared
    # statement"). Keep DROP and CREATE as separate executes.
    op.execute(_SET_UPDATED_AT_FN)
    for table in _MUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table}")
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_set_updated_at
                BEFORE UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at()
            """
        )

    op.execute(_AUDIT_IMMUTABLE_FN)
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs")
    op.execute(
        """
        CREATE TRIGGER trg_audit_logs_immutable
            BEFORE UPDATE OR DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION fn_audit_logs_immutable()
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs")
        op.execute("DROP FUNCTION IF EXISTS fn_audit_logs_immutable() CASCADE")
        for table in _MUTABLE_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table}")
        op.execute("DROP FUNCTION IF EXISTS fn_set_updated_at() CASCADE")
    Base.metadata.drop_all(bind=bind)
