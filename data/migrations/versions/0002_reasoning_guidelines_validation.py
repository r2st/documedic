"""Phase 2-4 schema: reasoning sessions, intake, immutable clinical suggestions, clinician
decisions, guideline corpus, validation runs, safety reports.

Created from the shared ORM metadata (checkfirst — idempotent on a DB where 0001's metadata
create_all already produced these tables), then PostgreSQL safeguards:
  * fn_set_updated_at trigger on the new mutable tables (reasoning_sessions, guideline_chunks)
  * append-only immutability triggers on clinical_suggestions, clinician_decisions,
    validation_runs (Critical Safety Rule #7 — suggestions are never UPDATEd/DELETEd)

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from app.models import Base  # noqa: E402

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_TABLES = [
    "reasoning_sessions",
    "intake_questions",
    "intake_answers",
    "clinical_suggestions",
    "clinician_decisions",
    "guideline_chunks",
    "validation_runs",
    "safety_reports",
]
_NEW_MUTABLE = ["reasoning_sessions", "guideline_chunks"]
_IMMUTABLE = ["clinical_suggestions", "clinician_decisions", "validation_runs"]

_IMMUTABLE_FN_TMPL = """
CREATE OR REPLACE FUNCTION fn_{table}_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '{table} rows are immutable. UPDATE and DELETE are prohibited.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    tables = [Base.metadata.tables[name] for name in _NEW_TABLES]
    Base.metadata.create_all(bind=bind, tables=tables, checkfirst=True)

    if not is_pg:
        return

    # One command per op.execute: the asyncpg driver runs DDL through a prepared statement,
    # which rejects multi-command strings ("cannot insert multiple commands into a prepared
    # statement"). Keep DROP and CREATE as separate executes.
    for table in _NEW_MUTABLE:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table}")
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_set_updated_at
                BEFORE UPDATE ON {table}
                FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at()
            """
        )

    for table in _IMMUTABLE:
        op.execute(_IMMUTABLE_FN_TMPL.format(table=table))
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_immutable
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION fn_{table}_immutable()
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        for table in _IMMUTABLE:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
            op.execute(f"DROP FUNCTION IF EXISTS fn_{table}_immutable() CASCADE")
        for table in _NEW_MUTABLE:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table}")
    tables = [Base.metadata.tables[name] for name in reversed(_NEW_TABLES)]
    Base.metadata.drop_all(bind=bind, tables=tables, checkfirst=True)
