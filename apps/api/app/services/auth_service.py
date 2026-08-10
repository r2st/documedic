"""Authentication service: signup, login, refresh-token rotation, logout.

Every login/logout/refresh outcome — including failed attempts — is recorded to the
append-only audit log (access trail; also supports brute-force detection). A failed login
commits its audit row immediately because the caller's exception path rolls the session back
before the router's own commit ever runs (see app.db.session.get_db).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
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
)
from app.models.user import Account, Session
from app.schemas.auth import TokenResponse
from app.services.audit_service import AuditService


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

    async def login(
        self,
        email: str,
        password: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> TokenResponse:
        account = await self._get_by_email(email)
        if account is None or not verify_password(password, account.password_hash):
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
            payload={"ip_address": ip_address, "user_agent": user_agent},
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
        result = await self.db.execute(
            select(Session).where(Session.token_hash == token_hash, Session.is_revoked.is_(False))
        )
        session = result.scalar_one_or_none()
        if session is None:
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
