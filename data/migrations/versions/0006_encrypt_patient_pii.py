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

It is also the one revision in the chain that does not roll back — see ``downgrade`` and
``IRREVERSIBLE`` below. Every revision above it does.

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


IRREVERSIBLE = (
    "0006 cannot be reversed automatically. These columns now hold Fernet ciphertext, and "
    "narrowing them back to their plaintext types is not a type change — it is a decryption. "
    "PostgreSQL refuses TEXT -> DATE on date_of_birth without a USING clause even on an empty "
    "table, and there is no USING expression that turns a Fernet token into a date; on a "
    "populated table phone would additionally overflow VARCHAR(20), since a token for a ten-"
    "digit number is over a hundred characters. To roll back past this revision, decrypt the "
    "patients table with app.core.crypto first (a one-off script, with the key that wrote it), "
    "then apply the DDL by hand — deliberately, because the result is plaintext PII at rest and "
    "the DPDP obligations in CLAUDE.md do not permit that to happen as a side effect of "
    "`alembic downgrade`."
)


def downgrade() -> None:
    """Refuse, rather than fail partway through.

    This body used to issue the three ``alter_column`` calls that mirror ``upgrade``. None of
    them could ever have succeeded — the failure is in PostgreSQL's type system, not in the
    data — so what the chain actually had was a downgrade that aborted mid-DDL with a driver-
    level ``DatatypeMismatchError`` about a USING clause, after ``full_name`` had already been
    narrowed. An operator rolling a bad release back was left with a half-downgraded
    ``patients`` table and a message about casting.

    Raising here costs nothing that worked before and changes what the operator is holding when
    it stops: the revision is still 0006, no DDL has run, and the message says what to do.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    raise RuntimeError(IRREVERSIBLE)
