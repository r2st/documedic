"""Portable SQLAlchemy column types.

Production runs on PostgreSQL 16 (UUID, JSONB, INET). Tests run on SQLite for
speed and isolation. These type decorators emit the native PostgreSQL type when
bound to a PG dialect and a portable fallback (CHAR/JSON/VARCHAR) on SQLite, so
the same ORM models serve both targets.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

from sqlalchemy import CHAR, String, Text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.types import JSON, TypeDecorator

from app.core.crypto import InvalidToken, decrypt_str, encrypt_str


class GUID(TypeDecorator):
    """Platform-independent UUID type.

    Uses PostgreSQL's native UUID, otherwise stores as a 32-char hex string.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(32))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        if isinstance(value, uuid.UUID):
            return value.hex
        return uuid.UUID(str(value)).hex

    def process_result_value(self, value: Any, dialect: Any) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


# JSONB on PostgreSQL, generic JSON elsewhere.
JSONBType = JSON().with_variant(JSONB(), "postgresql")


class INETType(TypeDecorator):
    """INET on PostgreSQL, plain VARCHAR on SQLite."""

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(INET())
        return dialect.type_descriptor(String(64))


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)


class EncryptedString(TypeDecorator):
    """Fernet-encrypted text column (application-level encryption at rest).

    Stored as ciphertext (base64 token, longer than the plaintext) so the underlying column
    must be ``Text``, not a bounded ``String`` — length constraints on the plaintext (e.g.
    ``full_name`` max 500 chars) stay enforced at the Pydantic schema layer instead.

    A value that fails to decrypt (wrong/rotated key, or a legacy plaintext row written before
    encryption was enabled on this column) is returned as-is rather than raising, matching this
    project's "never crash the UI on bad stored data" policy — the field just reads back
    un-decrypted instead of taking the whole request down.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return encrypt_str(str(value))

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        try:
            return decrypt_str(value)
        except (InvalidToken, ValueError):
            return value


class EncryptedDate(TypeDecorator):
    """Fernet-encrypted date column. See ``EncryptedString`` for the decrypt-failure policy."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = date.fromisoformat(value)
        return encrypt_str(value.isoformat())

    def process_result_value(self, value: Any, dialect: Any) -> date | None:
        if value is None:
            return None
        try:
            raw = decrypt_str(value)
        except (InvalidToken, ValueError):
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
