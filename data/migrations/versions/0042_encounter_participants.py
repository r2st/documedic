"""Who took part in one consultation, besides the account that owns the chart.

Two things the table does, and only one of them is about permissions: it records who wrote a
note, who supervised it, who was asked for an opinion and who was sitting in — none of which
``encounters``' single ``signed_by_account_id`` could say — and it grants access to that one
encounter, which is the setting between "the whole panel" and "nothing" that did not previously
exist.

Three column decisions worth stating here rather than leaving to be re-derived.

**``removed_at``/``removal_reason`` instead of a delete.** Access follows ``removed_at IS NULL``
and stops the moment it is set. What survives is the statement that this person had access
between these two times, which is the only form of the answer an access review can use; a
deleted row answers "who can see this now" and destroys "who could see it in March". The
constraint requires the pair together, because a withdrawn access with no recorded reason is
reliably the half of a review somebody needs.

**``purpose`` is NOT NULL.** An access grant to a clinical record with no stated purpose is the
row that cannot be justified afterwards. DPDP purpose limitation as a column rather than as a
policy — the same judgement that makes ``drug_safety_overrides.reasoning`` required.

**``granted_by_account_id`` is stored, not inferred.** Only the chart's owner may grant, so it
is derivable from ``patients.account_id`` *today*; ownership is a mutable fact and the review
asks who granted this at the time.

The unique index is partial on ``removed_at IS NULL``: one live participation per account per
encounter, with any number of historical spells behind it. A colleague brought in, removed, and
brought back a month later is two spells, and collapsing them would lose the gap — which is the
part of the history a review is looking for.

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_guards import index_exists

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "encounter_participants"
_LIVE_UNIQUE = "uq_encounter_participants_live"
_BY_ENCOUNTER = "ix_encounter_participants_encounter"
_BY_ACCOUNT = "ix_encounter_participants_account"

# Spelled out literally rather than interpolated from ``app.core.encounter_roles``: a migration
# is a record of what the schema became on the day it ran, and a constraint whose text moves
# with a later edit to application code is a migration that no longer describes the database it
# produced. ``tests/test_multi_provider_encounter.py`` holds the two together.
_ROLE_VALUES = "'author', 'supervising', 'consulting', 'observing'"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 0001 builds the schema from live ORM metadata, so a database created today already carries
    # this table and a bare create would abort the upgrade. Same guard as 0036/0037/0040/0041.
    if not sa.inspect(bind).has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "encounter_id",
                sa.Uuid(),
                sa.ForeignKey("encounters.id"),
                nullable=False,
            ),
            sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("role", sa.String(length=20), nullable=False),
            sa.Column(
                "granted_by_account_id",
                sa.Uuid(),
                sa.ForeignKey("accounts.id"),
                nullable=False,
            ),
            sa.Column("purpose", sa.Text(), nullable=False),
            sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("removal_reason", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.CheckConstraint(f"role IN ({_ROLE_VALUES})", name="ck_encounter_participants_role"),
            sa.CheckConstraint(
                "(removed_at IS NULL) = (removal_reason IS NULL)",
                name="ck_encounter_participants_removal_complete",
            ),
        )

    if not index_exists(bind, _TABLE, _LIVE_UNIQUE):
        op.execute(
            f"CREATE UNIQUE INDEX {_LIVE_UNIQUE} ON {_TABLE} (encounter_id, account_id) "
            "WHERE removed_at IS NULL"
        )
    if not index_exists(bind, _TABLE, _BY_ENCOUNTER):
        op.execute(f"CREATE INDEX {_BY_ENCOUNTER} ON {_TABLE} (encounter_id)")
    if not index_exists(bind, _TABLE, _BY_ACCOUNT):
        op.execute(f"CREATE INDEX {_BY_ACCOUNT} ON {_TABLE} (account_id, created_at DESC)")


def downgrade() -> None:
    """Drops the table, and with it every record of who took part in a consultation.

    Note what this is *not*: it is not a revocation. Dropping the table removes the grants, so
    access narrows rather than widens — which is the safe direction. What is destroyed is the
    history: who supervised which note, who was asked for an opinion, and who held access
    between which dates. An operator rolling back across this on a database in clinical use
    should dump it first, because that history is the answer to an access review and cannot be
    reconstructed from anything else.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for index in (_LIVE_UNIQUE, _BY_ENCOUNTER, _BY_ACCOUNT):
        op.execute(f"DROP INDEX IF EXISTS {index}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
