"""``sessions`` grew forever, and what it grew was credential material.

Nothing ever removed a row. A row is written on every sign-in *and on every refresh* — rotation
issues a new one and revokes the old — so one clinician who stays signed in contributes a row
every idle-timeout window for as long as they are working, and every one of those rows keeps a
token hash, a client address and a user agent long after it can authenticate anything. That is
unbounded growth of personal data with no purpose, which is the DPDP Act's data-minimisation
principle failing in the plainest way it can.

The sweep is deliberately conservative about what it will delete, because deleting a live
session signs a clinician out mid-consultation:

* only rows past their **absolute expiry** — the one state ``AuthService.refresh`` itself
  treats as unusable — and only once that expiry is older than the retention window;
* and only once ``updated_at`` is too, so a row revoked moments ago on an already-old expiry
  survives long enough to be read during an incident.

It is bounded per pass and it never fails a sign-in. And it removes an *access* record, not a
clinical one: the append-only audit log carries every sign-in, refresh, logout and revocation
independently and is never pruned.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.core.security import hash_token
from app.models.audit_log import AuditLog
from app.models.user import Account, Session
from app.services import auth_service as auth_module
from app.services.auth_service import AuthService

PREFIX = "/api/v1/auth"


async def _account(db) -> Account:
    account = Account(email=f"{uuid.uuid4().hex}@example.org", password_hash="x")
    db.add(account)
    await db.flush()
    return account


async def _session(db, account: Account, *, age_days: float, revoked: bool = False) -> Session:
    """A session row whose expiry (and last modification) sit ``age_days`` in the past."""
    when = datetime.now(UTC) - timedelta(days=age_days)
    row = Session(
        account_id=account.id,
        token_hash=hash_token(uuid.uuid4().hex),
        expires_at=when,
        last_used_at=when,
        family_started_at=when,
        is_revoked=revoked,
        created_at=when,
        updated_at=when,
    )
    db.add(row)
    await db.flush()
    return row


async def _count(db) -> int:
    return int(await db.scalar(select(func.count()).select_from(Session)) or 0)


@pytest.mark.asyncio
async def test_a_session_long_past_its_expiry_is_removed(db):
    account = await _account(db)
    await _session(db, account, age_days=settings.session_retention_days + 1)

    assert await AuthService(db).purge_expired_sessions() == 1
    await db.commit()
    assert await _count(db) == 0


@pytest.mark.asyncio
async def test_a_session_inside_the_retention_window_is_kept(db):
    """Recently dead is what an incident review reads."""
    account = await _account(db)
    await _session(db, account, age_days=1, revoked=True)

    assert await AuthService(db).purge_expired_sessions() == 0
    await db.commit()
    assert await _count(db) == 1


@pytest.mark.asyncio
async def test_a_live_session_is_never_removed_however_old_the_row(db):
    """The failure that would matter: signing a clinician out mid-consultation. A row created
    long ago but still inside its absolute expiry can still authenticate, and the sweep's
    selection is exactly the comparison `refresh` uses to reject one."""
    account = await _account(db)
    old = datetime.now(UTC) - timedelta(days=settings.session_retention_days + 90)
    db.add(
        Session(
            account_id=account.id,
            token_hash=hash_token(uuid.uuid4().hex),
            expires_at=datetime.now(UTC) + timedelta(days=1),
            last_used_at=old,
            family_started_at=old,
            created_at=old,
            updated_at=old,
        )
    )
    await db.flush()

    assert await AuthService(db).purge_expired_sessions() == 0
    await db.commit()
    assert await _count(db) == 1


@pytest.mark.asyncio
async def test_a_row_revoked_moments_ago_survives_its_old_expiry(db):
    """``updated_at`` moves when ``is_revoked`` flips, and both conditions are required, so
    "revoked five minutes ago" is not deletable merely because the expiry it carried was
    already stale."""
    account = await _account(db)
    row = await _session(db, account, age_days=settings.session_retention_days + 5)
    row.is_revoked = True
    row.updated_at = datetime.now(UTC)
    await db.flush()

    assert await AuthService(db).purge_expired_sessions() == 0


@pytest.mark.asyncio
async def test_the_sweep_is_bounded_per_pass(db, monkeypatch):
    """A deployment that has never swept catches up over several passes rather than issuing one
    enormous delete on a clinician's sign-in."""
    monkeypatch.setattr(settings, "session_sweep_batch_size", 3)
    account = await _account(db)
    for _ in range(7):
        await _session(db, account, age_days=settings.session_retention_days + 2)

    service = AuthService(db)
    assert await service.purge_expired_sessions() == 3
    await db.commit()
    assert await service.purge_expired_sessions() == 3
    await db.commit()
    assert await service.purge_expired_sessions() == 1
    await db.commit()
    assert await _count(db) == 0


@pytest.mark.asyncio
async def test_a_sweep_that_removed_rows_says_so_in_the_audit_trail(db):
    account = await _account(db)
    await _session(db, account, age_days=settings.session_retention_days + 2)
    await AuthService(db).purge_expired_sessions()
    await db.commit()

    actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "auth_sessions_purged" in actions


@pytest.mark.asyncio
async def test_a_sweep_that_removed_nothing_writes_no_audit_row(db):
    """Otherwise the trail fills with hourly no-ops and the real one stops standing out."""
    before = int(await db.scalar(select(func.count()).select_from(AuditLog)) or 0)
    await AuthService(db).purge_expired_sessions()
    await db.commit()
    assert int(await db.scalar(select(func.count()).select_from(AuditLog)) or 0) == before


@pytest.mark.asyncio
async def test_retention_can_be_switched_off(db, monkeypatch):
    monkeypatch.setattr(settings, "session_retention_days", 0)
    account = await _account(db)
    await _session(db, account, age_days=9999)
    assert await AuthService(db).purge_expired_sessions() == 0


# --- Scheduling -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sweep_runs_at_most_once_per_interval(db, monkeypatch):
    """Hung off the sign-in path because this deployment has no scheduler; without the interval
    every refresh would issue a delete."""
    account = await _account(db)
    for _ in range(2):
        await _session(db, account, age_days=settings.session_retention_days + 2)
    await db.commit()

    service = AuthService(db)
    assert await service.sweep_sessions_if_due() == 2
    await _session(db, account, age_days=settings.session_retention_days + 2)
    await db.commit()
    assert await service.sweep_sessions_if_due() == 0
    assert await _count(db) == 1

    monkeypatch.setattr(settings, "session_sweep_interval_minutes", 0)
    assert await service.sweep_sessions_if_due() == 0


@pytest.mark.asyncio
async def test_a_failing_sweep_never_fails_the_sign_in(client, monkeypatch, caplog):
    """Housekeeping that takes authentication down with it is worse than an unswept table."""

    async def boom(self, **_kwargs):
        raise RuntimeError("delete blew up")

    monkeypatch.setattr(AuthService, "purge_expired_sessions", boom)

    with caplog.at_level("WARNING"):
        resp = await client.post(
            f"{PREFIX}/signup",
            json={"email": "unbothered@example.org", "password": "correct-horse-battery"},
        )
        assert resp.status_code == 201
        login = await client.post(
            f"{PREFIX}/login",
            json={"email": "unbothered@example.org", "password": "correct-horse-battery"},
        )

    assert login.status_code == 200
    assert any("retention sweep failed" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_signing_in_sweeps(client, db):
    """End to end through the route, which is the only place the sweep is wired."""
    account = await _account(db)
    await _session(db, account, age_days=settings.session_retention_days + 2)
    await db.commit()

    await client.post(
        f"{PREFIX}/signup",
        json={"email": "sweeper@example.org", "password": "correct-horse-battery"},
    )
    await client.post(
        f"{PREFIX}/login",
        json={"email": "sweeper@example.org", "password": "correct-horse-battery"},
    )

    stale = await db.scalar(
        select(func.count()).select_from(Session).where(Session.account_id == account.id)
    )
    assert stale == 0


def test_the_schedule_stamp_is_reset_between_tests():
    """The autouse fixture that makes every test above deterministic — asserted so that
    removing it fails loudly rather than by silently skipping sweeps."""
    assert auth_module._last_session_sweep is None
