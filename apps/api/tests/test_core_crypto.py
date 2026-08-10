"""Unit tests for field-level encryption (app.core.crypto), independent of the DB."""

from __future__ import annotations

from cryptography.fernet import InvalidToken

from app.core.crypto import decrypt_str, encrypt_str


def test_round_trip():
    assert decrypt_str(encrypt_str("Ramesh Kumar")) == "Ramesh Kumar"


def test_ciphertext_is_not_plaintext():
    token = encrypt_str("Ramesh Kumar")
    assert token != "Ramesh Kumar"
    assert "Ramesh" not in token


def test_encryption_is_nondeterministic():
    """Fernet includes a random IV, so encrypting the same value twice differs — this is why
    patient search can't run as SQL ILIKE any more (see PatientService.list)."""
    assert encrypt_str("Ramesh Kumar") != encrypt_str("Ramesh Kumar")


def test_decrypting_garbage_raises_invalid_token():
    """Callers (EncryptedString/EncryptedDate) rely on this to detect legacy plaintext rows."""
    try:
        decrypt_str("not-a-real-fernet-token")
    except InvalidToken:
        pass
    else:
        raise AssertionError("expected InvalidToken")
