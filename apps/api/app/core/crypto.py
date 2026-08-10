"""Field-level symmetric encryption for patient PII at rest (Fernet/AES-128-CBC + HMAC).

Used by the ``EncryptedString``/``EncryptedDate`` column types in app.db.types — application
code never calls this module directly for patient data, it just uses those types on the model.

Key derivation: when ``settings.field_encryption_key`` is unset (dev convenience), a key is
derived from ``app_secret_key`` via SHA-256. Set ``FIELD_ENCRYPTION_KEY`` explicitly in
production so rotating the JWT signing secret doesn't also make stored PII undecryptable.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

__all__ = ["InvalidToken", "decrypt_str", "encrypt_str"]


@lru_cache
def _fernet() -> Fernet:
    key_material = settings.field_encryption_key or settings.app_secret_key
    derived = hashlib.sha256(key_material.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_str(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_str(token: str) -> str:
    """Raises ``InvalidToken`` if ``token`` isn't a value this key encrypted (wrong key, or
    plaintext left over from before encryption was enabled) — callers decide how to handle
    that rather than this module masking it."""
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
