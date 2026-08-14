"""Password hashing and JWT helpers — pure, deterministic, unit-testable."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.config import settings
from app.exceptions import TokenError


def hash_password(plain: str) -> str:
    """bcrypt hash. bcrypt has a 72-byte input limit, so long passwords are truncated.

    Synchronous and CPU-bound: at the configured 12 rounds this occupies a core for roughly a
    quarter of a second. Anything running on the event loop must call ``hash_password_async``.
    """
    pw = plain.encode("utf-8")[:72]
    return bcrypt.hashpw(pw, bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time bcrypt comparison. Fails closed on any malformed input.

    A corrupted, empty, or non-string stored hash must return False rather than raise —
    an exception here would turn a bad row into a 500 on the login path (and, with the
    dummy-hash enumeration defence in AuthService, into a crash on unknown emails).

    Synchronous and CPU-bound, like ``hash_password``; on the event loop call
    ``verify_password_async``.
    """
    if not isinstance(plain, str) or not isinstance(hashed, str):
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# bcrypt is deliberately slow -- that is the entire point of it -- and it is slow by burning CPU
# in a tight loop, not by waiting on anything. Called straight from an async handler it therefore
# stops the event loop dead: measured at the configured 12 rounds, ~265ms for a hash and ~380ms
# for a verification, during which *every* other request this worker is serving makes no progress
# at all, including the SSE streams carrying live reasoning output to a clinician mid-consultation.
#
# The login path made that worse than one stall per sign-in. Its account-enumeration defence runs
# a verification against a dummy hash even when the email is unknown, by design, so unauthenticated
# requests to a nonexistent address each freeze the worker for the better part of a second, one
# after another. That turns a defence into a cheap way to stall the API.
#
# Off-loading to a worker thread is what the rest of the codebase already does with its CPU-bound
# and blocking work (``agents.util`` for LLM calls, ``document_service`` for extraction,
# ``guideline_ingest`` for embedding). bcrypt releases the GIL while it hashes, so the threads
# genuinely run in parallel, and the loop stays free to serve everything else while they do.
#
# Timing symmetry survives: both branches of the login check go through this same wrapper, so the
# enumeration defence is unchanged -- the cost is paid, it is simply paid somewhere that does not
# hold up the rest of the process.


async def hash_password_async(plain: str) -> str:
    """``hash_password`` off the event loop. Use this from any async caller."""
    return await asyncio.to_thread(hash_password, plain)


async def verify_password_async(plain: str, hashed: str) -> bool:
    """``verify_password`` off the event loop. Use this from any async caller."""
    return await asyncio.to_thread(verify_password, plain, hashed)


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(account_id: str | uuid.UUID, *, extra: dict | None = None) -> str:
    """Short-lived signed JWT access token (HS256)."""
    now = _now()
    payload: dict[str, Any] = {
        "sub": str(account_id),
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_access_ttl_minutes)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.app_secret_key, algorithm=settings.jwt_algorithm)


def create_stream_token(account_id: str | uuid.UUID, session_id: str | uuid.UUID) -> str:
    """Narrowly scoped token for the SSE endpoint.

    EventSource cannot send an Authorization header, so the token travels in the query
    string — where nginx's default log format records it and the browser keeps it in
    history. This token is therefore useless for anything else: it is bound to one
    reasoning session (``sid``), carries its own ``type``, and expires in
    ``settings.stream_token_ttl_seconds``. The real access token never enters a URL.
    """
    now = _now()
    payload: dict[str, Any] = {
        "sub": str(account_id),
        "sid": str(session_id),
        "type": "stream",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.stream_token_ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT. Raises TokenError on any failure."""
    try:
        return jwt.decode(token, settings.app_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError(
            "Your sign-in session has expired. Sign in again to continue.",
            detail="jwt exp in the past",
        ) from exc
    except jwt.PyJWTError as exc:
        raise TokenError(detail=f"jwt rejected: {type(exc).__name__}") from exc


def generate_refresh_token() -> str:
    """Opaque high-entropy refresh token (stored only as a hash)."""
    return uuid.uuid4().hex + uuid.uuid4().hex


def hash_token(token: str) -> str:
    """SHA-256 of a token for storage/lookup. Raw tokens are never persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
