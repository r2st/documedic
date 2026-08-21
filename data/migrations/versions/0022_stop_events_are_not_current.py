"""Retire medication rows that record a discontinuation but are flagged as current.

``_merge_medication`` wrote ``is_current=True`` for every ``event_type``, including ``'stop'``.
``export_service`` had to work around it in the FHIR renderer — "a stop event is a stopped
medication whatever ``is_current`` says" — while the safety engine and ``GET /records``, which
read ``is_current`` and nothing else, went on treating the row as a drug the patient is taking.

The merge no longer produces such a row. Rows already written are corrected here, because a
chart is read by whatever is in it: until this runs, a patient whose record says a drug was
stopped is still listed as being on it, still pairs with everything started since for
interactions, and still counts toward the cumulative bleeding burden.

Scoped to the stop rows themselves and no further. Where a *discontinuation was dropped* rather
than mis-flagged — the more common shape, since the dedup key matched the very row the stop was
meant to retire and returned before the insert — there is nothing in the database to correct:
the line left no trace, and the drug it discontinued can only come back off the chart by
re-approving that document or by a clinician editing the record.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        "UPDATE medication_events SET is_current = false "
        "WHERE event_type = 'stop' AND is_current = true"
    )


def downgrade() -> None:
    """Deliberately empty.

    The reverse — marking discontinued medications current again — restores a defect rather than
    a schema, and nothing recorded which rows this touched. A downgrade that quietly puts
    patients back on drugs they were taken off is not a rollback anyone wants; the column is
    unchanged either way, so leaving the data corrected costs the earlier revision nothing.
    """
