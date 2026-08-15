"""Indexes for three reads that had none: two account-scoped lists and the reset-token sweep.

Migration 0008 indexed the patient-scoped hot paths and 0009 removed what those made redundant.
Three predicates were outside that sweep and stayed unindexed, each on a table that only grows:

* ``safety_reports (account_id, created_at DESC)`` — ``SafetyReportService.list_reports``
  reads exactly this filter and sort. The table already carried indexes on ``patient_id`` and
  ``severity``, which cover the other two ways of slicing the register and neither of which
  leads with ``account_id``, so the one read a clinician actually performs was the one doing a
  sequential scan. The register is append-only adverse-event evidence for the monitored pilot;
  nothing prunes it.

* ``validation_runs (account_id, created_at DESC)`` — ``ValidationService.list_runs``, same
  shape. This table had *no* index of any kind. Its rows are also the widest in the schema to
  scan: ``results`` holds per-vignette detail for a whole harness execution, so a sequential
  scan walks those heap pages to answer a filter that reads two narrow columns.

* ``password_reset_tokens (expires_at)`` — ``AuthService.purge_spent_reset_tokens`` selects
  ``WHERE expires_at < cutoff LIMIT n``. ``sessions.expires_at`` has carried this index since
  0001 for the identical sweep; the reset-token table was added later (0021) with the same shape
  and did not get one. It matters more here than there, for two reasons. The rows come from an
  *unauthenticated* endpoint, so it is the one auth table an outsider can inflate on purpose.
  And the sweep has no scheduler behind it — it runs inline on the sign-in path — so the scan is
  in front of a clinician logging in rather than in a background job.

None of the three is a leading-column prefix of an existing index on its table, so this adds no
redundancy for ``test_no_index_is_a_prefix_of_another`` to find.

Created CONCURRENTLY for the reason 0008 gives: these run against tables holding live data and a
plain CREATE INDEX holds a lock that blocks writes for its duration.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, index definition tail)
_INDEXES: list[tuple[str, str, str]] = [
    ("ix_safety_reports_account_created", "safety_reports", "(account_id, created_at DESC)"),
    ("ix_validation_runs_account_created", "validation_runs", "(account_id, created_at DESC)"),
    ("ix_password_reset_tokens_expires_at", "password_reset_tokens", "(expires_at)"),
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
