"""Encrypt patient PII columns at rest.

full_name, date_of_birth, phone, address_text, and notes on ``patients`` are now encrypted
application-side (Fernet, see app.core.crypto / app.db.types.EncryptedString/EncryptedDate)
before they ever reach the database. Ciphertext is longer than the plaintext and
non-deterministic, so the underlying columns widen to TEXT and the old ``phone`` index (which
can no longer accelerate any lookup on ciphertext) is dropped.

This migration only widens column TYPES for a database that already has the ``patients`` table
from an earlier revision — a fresh install creates the table straight from current ORM metadata
in 0001, already encrypted. It does NOT re-encrypt any pre-existing plaintext rows in an older
production database: EncryptedString/EncryptedDate read a plaintext (undecryptable) value back
unchanged/None rather than crashing, but those rows should be re-saved (e.g. via a one-off
backfill script) to actually get encrypted at rest.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_patients_phone")
    op.alter_column("patients", "full_name", type_=sa.Text(), existing_nullable=False)
    op.alter_column(
        "patients", "date_of_birth", type_=sa.Text(), existing_type=sa.Date(), existing_nullable=True
    )
    op.alter_column("patients", "phone", type_=sa.Text(), existing_nullable=True)
    # address_text / notes are already TEXT — no type change needed there.


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.alter_column("patients", "full_name", type_=sa.String(500), existing_nullable=False)
    op.alter_column(
        "patients", "date_of_birth", type_=sa.Date(), existing_type=sa.Text(), existing_nullable=True
    )
    op.alter_column("patients", "phone", type_=sa.String(20), existing_nullable=True)
    op.create_index("ix_patients_phone", "patients", ["phone"])
