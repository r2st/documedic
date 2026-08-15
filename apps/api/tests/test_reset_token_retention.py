"""The retention sweep was written for ``sessions`` and left the table beside it growing.

``password_reset_tokens`` has exactly the shape ``purge_expired_sessions`` exists to fix: one
row per request, marked ``used_at`` or ``invalidated_at`` when it is finished with, and then
kept forever. Nothing deleted one.

It is the worse of the two, for a reason the session table does not share. Writing a session row
requires a correct password; writing a reset row requires only knowing that an address has an
account, from an endpoint that is deliberately unauthenticated and deliberately silent. Five per
account per hour, indefinitely retained, is a table an outsider can grow — of token hashes,
request addresses and timestamps belonging to clinicians who never asked for anything.

The sweep here is keyed on ``expires_at`` alone. A token past its TTL cannot be spent whatever
its other columns say — ``reset_password`` refuses it on that comparison — so expiry is the one
condition that proves the row can never do anything again, and it catches the abandoned requests
that ``used_at`` and ``invalidated_at`` never will. What is removed is the capability's remains;
the history stays in the append-only audit log, which records every request, throttle,
completion and replay and is never pruned.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.core.security import hash_token
from app.models.audit_log import AuditLog
from app.models.user import Account, PasswordResetToken
from app.services.auth_service import AuthService, reset_sweep_schedule


async def _account(db) -> Account:
    account = Account(email=f"{uuid.uuid4().hex}@example.org", password_hash="x")
    db.add(account)
    await db.flush()
    return account


async def _token(
    db,
    account: Account,
    *,
    expired_days_ago: float,
    used: bool = False,
    invalidated: bool = False,
) -> PasswordResetToken:
    """A reset row whose TTL ran out ``expired_days_ago`` days back."""
    when = datetime.now(UTC) - timedelta(days=expired_days_ago)
    row = PasswordResetToken(
        account_id=account.id,
        token_hash=hash_token(uuid.uuid4().hex),
        expires_at=when,
        used_at=when if used else None,
        invalidated_at=when if invalidated else None,
    )
    db.add(row)
    await db.flush()
    return row


async def _count(db) -> int:
    return int(await db.scalar(select(func.count()).select_from(PasswordResetToken)) or 0)


@pytest.mark.asyncio
async def test_a_spent_token_past_the_retention_window_is_removed(db):
    account = await _account(db)
    await _token(db, account, expired_days_ago=settings.session_retention_days + 2, used=True)
    assert await AuthService(db).purge_spent_reset_tokens() == 1
    await db.commit()
    assert await _count(db) == 0


@pytest.mark.asyncio
async def test_an_abandoned_token_nobody_ever_spent_is_removed_too(db):
    """The row ``used_at``/``invalidated_at`` filtering would have kept forever.

    A reset link that was requested and never clicked is the commonest row in this table, and it
    is exactly as dead as a spent one the moment its TTL passes.
    """
    account = await _account(db)
    await _token(db, account, expired_days_ago=settings.session_retention_days + 2)
    assert await AuthService(db).purge_spent_reset_tokens() == 1


@pytest.mark.asyncio
async def test_a_live_token_is_never_removed(db):
    """Deleting one would break a reset a clinician is in the middle of, and the failure would
    be indistinguishable from the token having been wrong."""
    account = await _account(db)
    await _token(db, account, expired_days_ago=-1)  # expires tomorrow
    assert await AuthService(db).purge_spent_reset_tokens() == 0
    assert await _count(db) == 1


@pytest.mark.asyncio
async def test_a_token_inside_the_retention_window_is_kept(db):
    """Recently expired stays readable, so "someone asked for five resets on this account last
    week" is still answerable from the table during an incident."""
    account = await _account(db)
    await _token(db, account, expired_days_ago=1, used=True)
    assert await AuthService(db).purge_spent_reset_tokens() == 0


@pytest.mark.asyncio
async def test_the_sweep_is_bounded_per_pass(db, monkeypatch):
    """A deployment that has never swept catches up over several passes rather than issuing one
    enormous delete on a clinician's sign-in."""
    monkeypatch.setattr(settings, "session_sweep_batch_size", 2)
    account = await _account(db)
    for _ in range(5):
        await _token(db, account, expired_days_ago=settings.session_retention_days + 2, used=True)
    assert await AuthService(db).purge_spent_reset_tokens() == 2
    await db.commit()
    assert await _count(db) == 3


@pytest.mark.asyncio
async def test_retention_can_be_switched_off(db, monkeypatch):
    monkeypatch.setattr(settings, "session_retention_days", 0)
    account = await _account(db)
    await _token(db, account, expired_days_ago=9999, used=True)
    assert await AuthService(db).purge_spent_reset_tokens() == 0


@pytest.mark.asyncio
async def test_a_purge_that_removed_rows_says_so_in_the_audit_trail(db):
    """The rows cannot say they existed once they are gone, so the trail has to."""
    account = await _account(db)
    await _token(db, account, expired_days_ago=settings.session_retention_days + 2, used=True)
    await AuthService(db).purge_spent_reset_tokens()
    await db.commit()

    actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "auth_reset_tokens_purged" in actions


@pytest.mark.asyncio
async def test_a_purge_that_removed_nothing_writes_no_audit_row(db):
    before = int(await db.scalar(select(func.count()).select_from(AuditLog)) or 0)
    await AuthService(db).purge_spent_reset_tokens()
    await db.commit()
    assert int(await db.scalar(select(func.count()).select_from(AuditLog)) or 0) == before


@pytest.mark.asyncio
async def test_the_scheduled_sweep_covers_both_tables(db):
    """The sweep hangs off the sign-in paths, and there is only one of it.

    A second purge that had to be remembered separately is a purge that would be forgotten —
    which is how this table came to be missed in the first place.
    """
    reset_sweep_schedule()
    account = await _account(db)
    await _token(db, account, expired_days_ago=settings.session_retention_days + 2, used=True)
    assert await AuthService(db).sweep_sessions_if_due() == 1
    assert await _count(db) == 0
