"""Authentication service: signup, login, refresh-token rotation, logout.

Every login/logout/refresh outcome — including failed attempts — is recorded to the
append-only audit log (access trail; also supports brute-force detection). A failed login
commits its audit row immediately because the caller's exception path rolls the session back
before the router's own commit ever runs (see app.db.session.get_db).

Two abuse controls read that same trail, so both survive a process restart and need no Redis:

* **Login lockout** — more than ``settings.login_max_failed_attempts`` failures against one
  email (or from one IP) inside ``login_attempt_window_minutes`` locks further attempts for
  ``login_lockout_minutes``. The lockout is evaluated *before* the password is verified, so a
  locked-out attacker gets no bcrypt oracle at all.
* **Refresh-token reuse detection** — a refresh token is single-use (rotation revokes it).
  Presenting an already-revoked token is the classic stolen-token signal, so it revokes every
  live session for that account rather than merely failing the one call.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.exceptions import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    NotFoundError,
    TokenError,
    TooManyAttemptsError,
)
from app.models.audit_log import AuditLog
from app.models.user import Account, Session
from app.schemas.auth import TokenResponse
from app.services.audit_service import AuditService

# A bcrypt hash of a value no one can present, used to burn the same CPU time as a real
# verification when the email doesn't exist. Without it, "unknown email" returns markedly
# faster than "known email, wrong password" and the endpoint becomes an account-enumeration
# oracle. Generated once at import, not per call.
_DUMMY_PASSWORD_HASH = hash_password(uuid.uuid4().hex)


def _aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; treat naive timestamps as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def _get_by_email(self, email: str) -> Account | None:
        result = await self.db.execute(
            select(Account).where(Account.email == email.lower(), Account.is_deleted.is_(False))
        )
        return result.scalar_one_or_none()

    async def signup(self, email: str, password: str, display_name: str | None) -> Account:
        if await self._get_by_email(email):
            raise EmailAlreadyExistsError()
        account = Account(
            email=email.lower(),
            password_hash=hash_password(password),
            display_name=display_name,
        )
        self.db.add(account)
        await self.db.flush()
        await self.audit.record(
            action="auth_signup",
            account_id=account.id,
            entity_type="account",
            entity_id=account.id,
            payload={"email": account.email},
        )
        return account

    async def _issue_tokens(
        self,
        account: Account,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> TokenResponse:
        refresh = generate_refresh_token()
        now = datetime.now(UTC)
        session = Session(
            account_id=account.id,
            token_hash=hash_token(refresh),
            expires_at=now + timedelta(days=settings.jwt_refresh_ttl_days),
            ip_address=ip_address,
            user_agent=user_agent,
            last_used_at=now,
        )
        self.db.add(session)
        await self.db.flush()
        access = create_access_token(account.id)
        return TokenResponse(
            access_token=access,
            refresh_token=refresh,
            expires_in=settings.jwt_access_ttl_minutes * 60,
        )

    async def _recent_auth_events(self, since: datetime) -> list[AuditLog]:
        """Auth login outcomes recorded since ``since``, newest first.

        Bounded by a hard row limit so a flood of failures can't turn the lockout check into
        an unbounded load. The window is short (minutes), so this stays a small, indexed scan.
        """
        result = await self.db.execute(
            select(AuditLog)
            .where(
                AuditLog.created_at >= since,
                or_(
                    AuditLog.action == "auth_login_failed",
                    AuditLog.action == "auth_login_success",
                ),
            )
            .order_by(AuditLog.sequence.desc())
            .limit(500)
        )
        return list(result.scalars().all())

    async def _assert_not_locked_out(self, email: str, ip_address: str | None) -> None:
        """Raise TooManyAttemptsError when this email or IP has burned its attempt budget.

        Counted from the audit log rather than an in-memory counter so the control holds
        across restarts and across API workers. Failures older than the most recent *success*
        for the same email are ignored — a legitimate sign-in resets the budget.
        """
        max_attempts = settings.login_max_failed_attempts
        if max_attempts <= 0:  # explicitly disabled
            return

        now = datetime.now(UTC)
        window_start = now - timedelta(minutes=settings.login_attempt_window_minutes)
        events = await self._recent_auth_events(window_start)

        needle_email = email.lower()
        failures: list[datetime] = []
        for event in events:  # newest first
            payload = event.payload or {}
            same_email = payload.get("email") == needle_email
            if event.action == "auth_login_success":
                # Only a success for THIS email clears its budget; another account's
                # success must not unlock the attacker's target.
                if event.account_id is not None and same_email:
                    break
                continue
            same_ip = ip_address is not None and payload.get("ip_address") == ip_address
            if same_email or same_ip:
                failures.append(_aware(event.created_at))

        if len(failures) < max_attempts:
            return
        # Locked until `login_lockout_minutes` after the most recent failure, so continued
        # hammering keeps extending the lock rather than resetting it.
        if failures[0] + timedelta(minutes=settings.login_lockout_minutes) > now:
            # Audited (and committed, like a failed login) so lockouts are visible to the
            # security trail rather than only to the caller.
            await self.audit.record(
                action="auth_login_locked_out",
                entity_type="account",
                payload={
                    "email": needle_email,
                    "ip_address": ip_address,
                    "failed_attempts": len(failures),
                },
            )
            await self.db.commit()
            raise TooManyAttemptsError(
                "Too many failed sign-in attempts. Try again in "
                f"{settings.login_lockout_minutes} minutes."
            )

    async def login(
        self,
        email: str,
        password: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> TokenResponse:
        # Evaluated before any password work so a locked-out caller gets no timing oracle.
        await self._assert_not_locked_out(email, ip_address)

        account = await self._get_by_email(email)
        # Always run a bcrypt verification, even for an unknown email, so response time does
        # not distinguish "no such account" from "wrong password" (account enumeration).
        password_ok = verify_password(
            password, account.password_hash if account else _DUMMY_PASSWORD_HASH
        )
        if account is None or not password_ok:
            # Committed immediately: the router's commit never runs once this raises, and a
            # failed-login trail is exactly the record we cannot afford to lose to a rollback.
            await self.audit.record(
                action="auth_login_failed",
                account_id=account.id if account else None,
                entity_type="account",
                payload={"email": email.lower(), "ip_address": ip_address},
            )
            await self.db.commit()
            raise InvalidCredentialsError()
        tokens = await self._issue_tokens(account, ip_address=ip_address, user_agent=user_agent)
        await self.audit.record(
            action="auth_login_success",
            account_id=account.id,
            entity_type="account",
            entity_id=account.id,
            # `email` is what _assert_not_locked_out matches on to clear this account's
            # failed-attempt budget; it is already present on auth_login_failed rows.
            payload={
                "email": account.email,
                "ip_address": ip_address,
                "user_agent": user_agent,
            },
        )
        return tokens

    async def refresh(
        self,
        refresh_token: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> TokenResponse:
        token_hash = hash_token(refresh_token)
        result = await self.db.execute(select(Session).where(Session.token_hash == token_hash))
        session = result.scalar_one_or_none()
        if session is None:
            raise TokenError("Refresh token is invalid or expired")
        if session.is_revoked:
            # Reuse detection. Rotation revokes a refresh token the moment it is exchanged, so
            # a second presentation means the token was captured (or replayed after logout).
            # We cannot tell the attacker's copy from the legitimate one, so revoke every live
            # session for the account and force a fresh sign-in.
            await self._revoke_on_reuse(session)
            raise TokenError("Refresh token is invalid or expired")
        now = datetime.now(UTC)
        if _aware(session.expires_at) < now:
            raise TokenError("Refresh token is invalid or expired")
        idle_cutoff = _aware(session.last_used_at) + timedelta(
            minutes=settings.session_idle_timeout_minutes
        )
        if idle_cutoff < now:
            # Idle too long: revoke and audit distinctly from a normal expiry/logout so this is
            # diagnosable (Session Management requirement, not merely absolute TTL expiry).
            session.is_revoked = True
            await self.db.flush()
            await self.audit.record(
                action="auth_session_idle_expired",
                account_id=session.account_id,
                entity_type="account",
                payload={"ip_address": ip_address, "session_id": str(session.id)},
            )
            await self.db.commit()
            raise TokenError("Session expired due to inactivity")

        # Rotate: revoke the presented token, issue a new pair.
        session.is_revoked = True
        await self.db.flush()
        account = await self.db.get(Account, session.account_id)
        if account is None or account.is_deleted:
            raise TokenError("Account no longer exists")
        tokens = await self._issue_tokens(account, ip_address=ip_address, user_agent=user_agent)
        await self.audit.record(
            action="auth_token_refreshed",
            account_id=account.id,
            entity_type="account",
            entity_id=account.id,
            payload={"ip_address": ip_address, "user_agent": user_agent},
        )
        return tokens

    async def _revoke_on_reuse(self, reused: Session) -> None:
        """Revoke the whole session family after a revoked refresh token is presented again.

        Committed immediately: the caller raises TokenError straight after, and the router's
        commit never runs once that propagates (app.db.session.get_db rolls back), so without
        this the revocations — and the security-relevant audit row — would be lost.
        """
        live = await self.list_sessions(reused.account_id)
        for session in live:
            session.is_revoked = True
        await self.db.flush()
        await self.audit.record(
            action="auth_refresh_token_reuse_detected",
            account_id=reused.account_id,
            entity_type="account",
            entity_id=reused.account_id,
            payload={
                "reused_session_id": str(reused.id),
                "revoked_count": len(live),
            },
        )
        await self.db.commit()

    async def logout(self, refresh_token: str) -> None:
        token_hash = hash_token(refresh_token)
        result = await self.db.execute(select(Session).where(Session.token_hash == token_hash))
        session = result.scalar_one_or_none()
        if session is not None:
            session.is_revoked = True
            await self.db.flush()
            await self.audit.record(
                action="auth_logout",
                account_id=session.account_id,
                entity_type="account",
                entity_id=session.account_id,
                payload={},
            )

    async def list_sessions(self, account_id: uuid.UUID) -> list[Session]:
        """Active (not revoked, not expired) sessions for an account, most recent first."""
        now = datetime.now(UTC)
        result = await self.db.execute(
            select(Session)
            .where(
                Session.account_id == account_id,
                Session.is_revoked.is_(False),
                Session.expires_at > now,
            )
            .order_by(Session.last_used_at.desc())
        )
        return list(result.scalars().all())

    async def revoke_session(self, account_id: uuid.UUID, session_id: uuid.UUID) -> None:
        """Revoke one of the account's own sessions (e.g. "sign out of that device")."""
        session = await self.db.get(Session, session_id)
        if session is None or session.account_id != account_id:
            raise NotFoundError("Session not found")
        session.is_revoked = True
        await self.db.flush()
        await self.audit.record(
            action="auth_session_revoked",
            account_id=account_id,
            entity_type="account",
            payload={"session_id": str(session_id)},
        )

    async def revoke_all_sessions(
        self, account_id: uuid.UUID, *, keep_refresh_token: str | None = None
    ) -> int:
        """Revoke every active session for an account, optionally keeping the caller's own
        current one alive. Returns the number of sessions revoked."""
        keep_hash = hash_token(keep_refresh_token) if keep_refresh_token else None
        sessions = await self.list_sessions(account_id)
        revoked = 0
        for session in sessions:
            if keep_hash is not None and session.token_hash == keep_hash:
                continue
            session.is_revoked = True
            revoked += 1
        if revoked:
            await self.db.flush()
            await self.audit.record(
                action="auth_logout_all",
                account_id=account_id,
                entity_type="account",
                payload={"revoked_count": revoked},
            )
        return revoked
