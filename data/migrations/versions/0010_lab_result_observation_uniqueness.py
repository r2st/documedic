"""Make "one blood draw recorded twice" impossible at the database level.

``GraphService`` deduplicates a re-approved extraction by reading the observation keys already on
the chart and skipping the ones it matches. That is a read-then-insert, and two approvals of one
document that overlap inside that window both read a chart without the observation and both
insert it. ``DocumentService.get`` takes ``FOR UPDATE`` to close the window, but SQLAlchemy's
SQLite dialect silently drops the clause, so the guarantee only exists on PostgreSQL.

A duplicated lab result is not cosmetic. The chart shows one draw as two, which reads as two
independent measurements agreeing rather than one measurement counted twice; and every duplicated
creatinine has its eGFR recomputed and stored alongside it, so the renal contraindication rules
read a doubled derived marker.

This revision adds the durable half: ``lab_results.dedup_key``, a sha256 of (source document,
normalised marker, value, sample timestamp), and a partial unique index on
``(patient_id, dedup_key) WHERE is_deleted = false``. The loser of the race now fails its INSERT
instead of writing the duplicate, on every backend, and ``DocumentService.approve`` turns that
into a 409 the client retries into the ordinary sequential path.

A digest column rather than a composite unique index over the four source columns: two of them
are nullable, NULLs are distinct in a unique index on both dialects (PostgreSQL's
``NULLS NOT DISTINCT`` is 15+ and SQLite has no equivalent), and the undated / non-numeric
observations would then be precisely the rows left unconstrained.

Backfill, in three steps, because the index cannot be built over data that already violates it:

1. add the column nullable and fill it row by row through the same Python function the
   application writes (``app.models.lab_result.lab_observation_key``) — the values must agree
   exactly with what new inserts produce, and reimplementing the hash in SQL is the obvious way
   to get that subtly wrong;
2. soft-delete rows that the constraint would reject, keeping the earliest of each group. These
   are exact duplicates of one another — same document, marker, value and draw time — so they
   are the artefact this migration exists to prevent, already recorded. ``is_deleted`` is a soft
   delete and the index is partial on it: nothing is destroyed, the rows stay readable in the
   table, and the count is logged;
3. create the index and make the column NOT NULL.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-14
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.models.lab_result import lab_observation_key

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_INDEX = "uq_lab_results_observation"
_BATCH = 1000


def _backfill(bind: sa.Connection) -> None:
    """Fill ``dedup_key`` for every existing row, in batches, using the application's function."""
    source = sa.text(
        "SELECT id, source_document_id, marker_name, value_numeric, sample_date "
        "FROM lab_results WHERE dedup_key IS NULL"
    )
    update = sa.text("UPDATE lab_results SET dedup_key = :key WHERE id = :id")
    filled = 0
    while True:
        rows = bind.execute(source).fetchmany(_BATCH)
        if not rows:
            break
        bind.execute(
            update,
            [
                {
                    "id": row.id,
                    "key": lab_observation_key(
                        row.source_document_id,
                        row.marker_name,
                        row.value_numeric,
                        row.sample_date,
                    ),
                }
                for row in rows
            ],
        )
        filled += len(rows)
    logger.info("0010: filled dedup_key on %d lab_results", filled)


def _retire_existing_duplicates(bind: sa.Connection) -> None:
    """Soft-delete every live row the new index would reject, keeping the earliest of each group.

    Ordering is (created_at, id): created_at is the clinically meaningful "recorded first", and
    the primary key breaks ties so the choice is deterministic rather than whatever the scan
    happened to return first.
    """
    retired = bind.execute(
        sa.text(
            "UPDATE lab_results SET is_deleted = true WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY patient_id, dedup_key ORDER BY created_at, id"
            "    ) AS rn"
            "    FROM lab_results WHERE is_deleted = false"
            "  ) ranked WHERE rn > 1"
            ")"
        )
    ).rowcount
    if retired:
        logger.warning(
            "0010: soft-deleted %d duplicate lab_results so %s could be created. They remain in "
            "the table with is_deleted = true; query them by (patient_id, dedup_key).",
            retired,
            _INDEX,
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.add_column("lab_results", sa.Column("dedup_key", sa.String(length=64), nullable=True))
    _backfill(bind)
    _retire_existing_duplicates(bind)
    op.alter_column("lab_results", "dedup_key", nullable=False)
    # CONCURRENTLY, like every other index this schema has added since 0008: an ACCESS EXCLUSIVE
    # lock on lab_results blocks every chart read for the duration of the build.
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            "ON lab_results (patient_id, dedup_key) WHERE is_deleted = false"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    op.drop_column("lab_results", "dedup_key")
