"""Bearer-token resolution and the JWT/password primitives underneath it.

get_current_account is the single gate in front of every patient endpoint, so each way a
token can be wrong needs to end in 401 rather than in an authenticated request.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sqlalchemy import select

from app.config import settings
from app.core.security import (
    create_access_token,
    decode_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.exceptions import TokenError
from app.models.user import Account


async def _auth_get(client, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return await client.get("/api/v1/auth/me", headers=headers)


# --- get_current_account ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_valid_access_token_resolves_the_account(auth_client):
    resp = await auth_client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    assert resp.json()["email"] == "doc@example.com"


@pytest.mark.asyncio
async def test_a_missing_authorization_header_is_401(client):
    assert (await _auth_get(client, None)).status_code == 401


@pytest.mark.asyncio
async def test_a_non_bearer_scheme_is_401(client):
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Basic abc123"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_a_garbage_token_is_401(client):
    assert (await _auth_get(client, "not-a-jwt")).status_code == 401


@pytest.mark.asyncio
async def test_an_expired_token_is_401(client, monkeypatch):
    monkeypatch.setattr(settings, "jwt_access_ttl_minutes", -1)
    assert (await _auth_get(client, create_access_token(uuid.uuid4()))).status_code == 401


@pytest.mark.asyncio
async def test_a_token_signed_with_the_wrong_key_is_401(client):
    """The signature is the whole control — a self-minted token must not be accepted."""
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "type": "access",
            "exp": int((datetime.now(UTC) + timedelta(minutes=10)).timestamp()),
        },
        "attacker-chosen-key",
        algorithm="HS256",
    )
    assert (await _auth_get(client, forged)).status_code == 401


@pytest.mark.asyncio
async def test_an_unsigned_none_algorithm_token_is_rejected(client):
    """Classic JWT downgrade attack: alg=none with no signature."""
    forged = jwt.encode({"sub": str(uuid.uuid4()), "type": "access"}, key="", algorithm="none")
    assert (await _auth_get(client, forged)).status_code == 401


@pytest.mark.asyncio
async def test_a_refresh_token_cannot_be_used_as_an_access_token(client):
    """Token confusion: the long-lived refresh credential must not open patient endpoints."""
    wrong_type = create_access_token(uuid.uuid4(), extra={"type": "refresh"})
    assert (await _auth_get(client, wrong_type)).status_code == 401


@pytest.mark.asyncio
async def test_a_token_with_a_non_uuid_subject_is_401(client):
    assert (await _auth_get(client, create_access_token("not-a-uuid"))).status_code == 401


@pytest.mark.asyncio
async def test_a_token_for_a_nonexistent_account_is_401(client):
    """A valid signature over a deleted/unknown account id is still not an identity."""
    assert (await _auth_get(client, create_access_token(uuid.uuid4()))).status_code == 401


@pytest.mark.asyncio
async def test_a_soft_deleted_account_can_no_longer_authenticate(auth_client, db):
    account = (
        await db.execute(select(Account).where(Account.email == "doc@example.com"))
    ).scalar_one()
    account.is_deleted = True
    await db.commit()

    assert (await auth_client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_surrounding_whitespace_in_the_header_is_tolerated(auth_client):
    token = auth_client.headers["Authorization"].split(" ", 1)[1]
    resp = await auth_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer  {token} "})
    assert resp.status_code == 200


# --- Password hashing ------------------------------------------------------------------


def test_password_round_trip():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True
    assert verify_password("wrong password", hashed) is False


def test_hashes_are_salted_so_the_same_password_hashes_differently():
    assert hash_password("same") != hash_password("same")


def test_passwords_beyond_bcrypts_72_byte_limit_do_not_raise():
    long_password = "p" * 200
    hashed = hash_password(long_password)
    assert verify_password(long_password, hashed) is True


def test_verify_password_returns_false_for_a_malformed_stored_hash():
    """Legacy or corrupted rows must fail closed, not raise a 500 on the login path."""
    assert verify_password("anything", "not-a-bcrypt-hash") is False
    assert verify_password("anything", "") is False


def test_verify_password_handles_a_non_string_hash():
    assert verify_password("anything", None) is False  # type: ignore[arg-type]


# --- Token helpers ---------------------------------------------------------------------


def test_access_tokens_carry_subject_type_and_expiry():
    account_id = uuid.uuid4()
    payload = decode_token(create_access_token(account_id))
    assert payload["sub"] == str(account_id)
    assert payload["type"] == "access"
    assert payload["exp"] > payload["iat"]


def test_each_access_token_gets_a_unique_jti():
    a = decode_token(create_access_token(uuid.uuid4()))
    b = decode_token(create_access_token(uuid.uuid4()))
    assert a["jti"] != b["jti"]


def test_extra_claims_are_merged():
    payload = decode_token(create_access_token(uuid.uuid4(), extra={"role": "clinician"}))
    assert payload["role"] == "clinician"


def test_decode_token_raises_on_a_tampered_payload():
    token = create_access_token(uuid.uuid4())
    head, payload, sig = token.split(".")
    with pytest.raises(TokenError):
        decode_token(f"{head}.{payload}x.{sig}")


def test_decode_token_reports_expiry_distinctly():
    expired = jwt.encode(
        {"sub": str(uuid.uuid4()), "type": "access", "exp": 1_000_000},
        settings.app_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError, match="expired"):
        decode_token(expired)


def test_refresh_tokens_are_high_entropy_and_unique():
    tokens = {generate_refresh_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) == 64 for t in tokens)


def test_token_hashing_is_deterministic_and_one_way():
    token = generate_refresh_token()
    assert hash_token(token) == hash_token(token)
    assert token not in hash_token(token)
    assert len(hash_token(token)) == 64
