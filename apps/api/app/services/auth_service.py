"""Authentication service: signup, login, refresh-token rotation, logout.

Every login/logout/refresh outcome — including failed attempts — is recorded to the
append-only audit log (access trail; also supports brute-force detection). A failed login
commits its audit row immediately because the caller's exception path rolls the session back
before the router's own commit ever runs (see app.db.session.get_db).
"""

from __future__ import annotations

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
    TokenError,
)
from app.models.user import Account, Session
from app.schemas.auth import TokenResponse
from app.services.audit_service import AuditService


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
        session = Session(
            account_id=account.id,
            token_hash=hash_token(refresh),
            expires_at=datetime.now(UTC) + timedelta(days=settings.jwt_refresh_ttl_days),
            ip_address=ip_address,
            user_agent=user_agent,
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
        # SQLite drops tzinfo on round-trip; treat naive timestamps as UTC.
        expires_at = session.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < datetime.now(UTC):
            raise TokenError("Refresh token is invalid or expired")

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
