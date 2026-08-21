"""Make "the same file uploaded twice into one chart" impossible at the database level.

``DocumentService.upload`` deduplicates on ``(patient_id, storage_hash_sha256)``: it reads the
chart for a live document with those bytes and inserts one only if there is none. That is a
read-then-insert, and two uploads of the same file that overlap inside that window both read a
chart without the document and both write one. A double-clicked upload button is enough, and so
is a client that retries after a timeout while the first request is still running.

The duplicate row is not the damage; how the next read handles it is. That lookup used
``scalar_one_or_none``, which raises ``MultipleResultsFound`` over a pair of matching rows — so
from the moment the race is lost, *every* subsequent upload of that file answers 500. That
includes the re-upload which is the documented recovery for a scan that would not read, leaving
a chart holding one document twice and no way to put the file in again short of altering its
bytes.

This revision adds the durable half: a partial unique index on
``(patient_id, storage_hash_sha256) WHERE is_deleted = false``. The loser of the race now fails
its INSERT instead of writing the duplicate, and ``upload`` turns that into a plain answer with
the winning document — which is what the second of two double-clicks should get.

Partial on ``is_deleted``, like ``uq_lab_results_observation``, so a withdrawn document does not
permanently forbid re-uploading the file it held.

Existing violations are soft-deleted, keeping the earliest of each group, because the index
cannot be built over data that already breaks it. The retired rows are exact duplicates —
identical bytes, identical chart — so they are the artefact this migration exists to prevent,
and they keep their extraction and their audit references. Nothing is destroyed, the rows stay
readable in the table, and the count is logged. Their blobs are left on disk untouched: the
surviving row of each group is content-addressed to the same bytes and usually the same path,
and a migration that deletes files is not one that can be rolled back.

The kept row is chosen by ``(created_at, id)`` — "uploaded first", with the primary key breaking
ties so the choice is deterministic rather than whatever the scan returned first. That is the
same order ``DocumentService._already_uploaded`` reads in, so the application and this migration
agree on which of a pre-existing pair is *the* document.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-15
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_INDEX = "uq_documents_patient_hash"


def _retire_existing_duplicates(bind: sa.Connection) -> None:
    """Soft-delete every live row the new index would reject, keeping the earliest of each group."""
    retired = bind.execute(
        sa.text(
            "UPDATE documents SET is_deleted = true WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY patient_id, storage_hash_sha256 ORDER BY created_at, id"
            "    ) AS rn"
            "    FROM documents WHERE is_deleted = false"
            "  ) ranked WHERE rn > 1"
            ")"
        )
    ).rowcount
    if retired:
        logger.warning(
            "0026: soft-deleted %d duplicate documents so %s could be created. They remain in the "
            "table with is_deleted = true; query them by (patient_id, storage_hash_sha256). Their "
            "stored files are untouched.",
            retired,
            _INDEX,
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _retire_existing_duplicates(bind)
    # CONCURRENTLY, like every index added since 0008: an ACCESS EXCLUSIVE lock on `documents`
    # blocks every chart read for the duration of the build. IF NOT EXISTS because 0001 builds
    # the schema from the live ORM models, so a database created today already has this index
    # and a bare CREATE would abort the upgrade.
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            "ON documents (patient_id, storage_hash_sha256) WHERE is_deleted = false"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
