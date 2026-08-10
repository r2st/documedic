"""Add drug_safety_overrides: documented clinician overrides of hard-blocked safety checks.

CLAUDE.md rule #3 requires hard blocks to be overridable only with explicit, documented
clinician reasoning -- never silently bypassed. Previously no such override path existed.
This table is the sanctioned path; it is append-only (same immutability pattern as
clinical_suggestions/clinician_decisions/validation_runs from 0002).

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-10
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.models import Base  # noqa: E402

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "drug_safety_overrides"

_IMMUTABLE_FN = f"""
CREATE OR REPLACE FUNCTION fn_{_TABLE}_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '{_TABLE} rows are immutable. UPDATE and DELETE are prohibited.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    Base.metadata.create_all(bind=bind, tables=[Base.metadata.tables[_TABLE]], checkfirst=True)

    if not is_pg:
        return

    op.execute(_IMMUTABLE_FN)
    op.execute(
        f"""
        DROP TRIGGER IF EXISTS trg_{_TABLE}_immutable ON {_TABLE};
        CREATE TRIGGER trg_{_TABLE}_immutable
            BEFORE UPDATE OR DELETE ON {_TABLE}
            FOR EACH ROW EXECUTE FUNCTION fn_{_TABLE}_immutable();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_immutable ON {_TABLE};")
        op.execute(f"DROP FUNCTION IF EXISTS fn_{_TABLE}_immutable();")
    op.drop_table(_TABLE)
