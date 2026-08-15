"""One answer of record per clarifying question, enforced by the database.

``ReasoningService.submit_answers`` has always treated a second answer to a question as a
correction that rewrites the first, and the reason is not tidiness. ``_rebuild_intake_state``
collapses answers with ``answers_by_q[a.question_id] = a.answer_text`` over an *unordered*
query, so two rows for one question mean the one that reaches the engine is whatever order the
database returned. That is clinical input: ``agents.util.text_blob`` feeds affirmative answers to
the can't-miss sentinel and drops the keywords of anything answered "no", so on a ``red_flag``
question it decides whether a time-critical diagnosis is screened in or out. And the two rows
cannot be told apart after the fact — rows written in one transaction share a ``created_at`` and
the primary key is a random UUID rather than a sequence, so "the latest answer" is not a question
the schema can answer.

The rule was enforced only in Python, by reading the stored answers and updating the row it
found. That is a read-then-insert, and two submissions overlapping inside the window both read no
answer and both insert. The ordinary ways to produce a second submission are a double-clicked
Submit, a retried request, and the same case open in two rooms — the arrangement this product is
built for. A "yes" and a "no" to one red-flag question then sat in the table together.

This revision adds the durable half: a unique index on ``intake_answers (question_id)``. The
loser of the race is refused rather than written, on every backend, and ``submit_answers`` rolls
back, re-reads, and applies its answer over the winner's row as the correction it was.

Backfill first, because the index cannot be built over data that already violates it. Existing
duplicates are the artefact this exists to prevent and are already recorded; the *earliest* of
each group is kept, by (created_at, id) — created_at is "answered first" and the primary key
breaks the ties that a shared transaction timestamp creates, so the choice is deterministic
rather than whatever the scan returned. ``intake_answers`` has no soft-delete column, so the
losers are deleted rather than retired; the count is logged, and what is discarded is by
construction an answer the engine was already ignoring at random.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-15
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_INDEX = "uq_intake_answers_question"


def _drop_existing_duplicates(bind: sa.Connection) -> None:
    """Delete every row the new index would reject, keeping the earliest answer per question."""
    dropped = bind.execute(
        sa.text(
            "DELETE FROM intake_answers WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY question_id ORDER BY created_at, id"
            "    ) AS rn"
            "    FROM intake_answers"
            "  ) ranked WHERE rn > 1"
            ")"
        )
    ).rowcount
    if dropped:
        logger.warning(
            "0024: deleted %d duplicate intake_answers so %s could be created. Each was a second "
            "answer to a question that already had one, and which of the two the reasoning engine "
            "read was already undefined.",
            dropped,
            _INDEX,
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _drop_existing_duplicates(bind)
    # CONCURRENTLY, like every index this schema has added since 0008: an ACCESS EXCLUSIVE lock
    # on intake_answers blocks the intake loop of every reasoning session in flight.
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            "ON intake_answers (question_id)"
        )
        # The plain index the column carried is now a strict prefix of a unique index on the same
        # column — it answers nothing the new one does not, and costs a write on every answer
        # recorded. Same argument as migration 0009. Dropped after the unique index exists so no
        # lookup on question_id is ever unindexed.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_intake_answers_question_id")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_intake_answers_question_id "
            "ON intake_answers (question_id)"
        )
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
