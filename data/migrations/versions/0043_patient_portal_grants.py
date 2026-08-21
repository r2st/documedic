"""Time-limited, revocable, read-only credentials letting a patient see their own record.

Not accounts. A patient does not sign up here and has no row in ``accounts``: a clinician issues
a credential against one chart and can withdraw it. An account-based patient login would need a
second identity system, a second recovery flow and a second lockout policy, each of which is
another way to reach a medical record.

``token_hash`` holds a SHA-256 of an opaque random string — never a JWT, and never the token
itself. The hash is what makes a database dump not a set of working credentials to living
patients' records. The opacity is what keeps the two authentication paths in this API from ever
being confused: ``app.core.security.decode_token`` cannot parse a random string, so a patient's
credential can never be accepted by a clinician's route, and the reverse holds for the same
reason. That is a stronger guarantee than checking a ``type`` claim, because it survives edits
to either path.

``expires_at`` is NOT NULL by design. A read-only link to a medical record that never expires is
one that outlives the reason it was issued, the device it was opened on, and eventually the
clinical relationship. The ninety-day ceiling is in the service; the column is what makes
"forever" unrepresentable.

``revoked_at``/``revocation_reason`` come as a pair, like ``encounter_participants``: a withdrawn
credential with no recorded reason is the half of an access review somebody needs.

The unique index on ``token_hash`` is the authentication lookup and a correctness statement in
one — two grants sharing a hash would be two credentials nobody could tell apart. With 256 bits
of entropy behind each token, a collision here is a broken generator rather than bad luck, which
is exactly the thing a unique index should be relied on to surface.

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_guards import index_exists

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "patient_portal_grants"
_TOKEN_UNIQUE = "uq_patient_portal_grants_token"
_BY_PATIENT = "ix_patient_portal_grants_patient"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 0001 builds the schema from live ORM metadata, so a database created today already carries
    # this table and a bare create would abort the upgrade. Same guard as 0036/0037/0040-0042.
    if not sa.inspect(bind).has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("patients.id"), nullable=False),
            sa.Column(
                "issued_by_account_id",
                sa.Uuid(),
                sa.ForeignKey("accounts.id"),
                nullable=False,
            ),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=200), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revocation_reason", sa.Text(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
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
            sa.CheckConstraint(
                "(revoked_at IS NULL) = (revocation_reason IS NULL)",
                name="ck_patient_portal_grants_revocation_complete",
            ),
        )

    if not index_exists(bind, _TABLE, _TOKEN_UNIQUE):
        op.execute(f"CREATE UNIQUE INDEX {_TOKEN_UNIQUE} ON {_TABLE} (token_hash)")
    if not index_exists(bind, _TABLE, _BY_PATIENT):
        op.execute(f"CREATE INDEX {_BY_PATIENT} ON {_TABLE} (patient_id, created_at DESC)")


def downgrade() -> None:
    """Drops the table, and with it every issued portal credential.

    This one rolls back safely in the direction that matters: with the table gone, no portal
    token authenticates, so every patient link stops working at once rather than any of them
    widening. What is lost is the history — which patients were given access to their record,
    by whom, when it expired and why it was withdrawn — and that is the answer to a DPDP
    subject-access question, so an operator rolling back on a database in clinical use should
    dump it first.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for index in (_TOKEN_UNIQUE, _BY_PATIENT):
        op.execute(f"DROP INDEX IF EXISTS {index}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
