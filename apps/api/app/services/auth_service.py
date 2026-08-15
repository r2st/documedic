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

A sign-in also has an absolute end. Rotation carries ``family_started_at`` forward and anchors
each new row's ``expires_at`` to it, so ``jwt_refresh_ttl_days`` bounds the sign-in rather than
the newest token in it — see :meth:`AuthService._issue_tokens`.
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
    hash_password_async,
    hash_token,
    verify_password_async,
)
from app.exceptions import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    NotFoundError,
    SessionExpiredError,
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
# oracle. Generated once at import, not per call -- and with the synchronous `hash_password`,
# because at import time there is no event loop yet to keep clear of.
_DUMMY_PASSWORD_HASH = hash_password(uuid.uuid4().hex)

# Hard row cap on one lockout dimension's history read. The query is already scoped to a
# single email or a single address inside a window of minutes, so this is far above any real
# budget and exists only to keep a flood from turning the check into an unbounded scan.
_MAX_ATTEMPTS_SCANNED = 500


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
            password_hash=await hash_password_async(password),
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
        family_started_at: datetime | None = None,
    ) -> TokenResponse:
        """Mint an access/refresh pair, as a new sign-in or as the next link in a family.

        ``family_started_at`` is the moment the family's password sign-in happened, passed in
        by :meth:`refresh` and left unset by :meth:`login`. ``expires_at`` is anchored to it
        rather than to now, which is what makes ``jwt_refresh_ttl_days`` an absolute ceiling
        on a sign-in instead of a rolling one on whichever token is currently newest.

        Without that anchor a client refreshing inside the idle timeout renewed its own
        absolute expiry on every rotation, so a session never ended and a refresh token taken
        off a device kept working for as long as its holder cared to keep rotating it — reuse
        detection cannot see that, because it needs the *legitimate* client to present the
        stale token, and a device that has been put away never does.
        """
        refresh = generate_refresh_token()
        now = datetime.now(UTC)
        started = _aware(family_started_at) if family_started_at else now
        session = Session(
            account_id=account.id,
            token_hash=hash_token(refresh),
            expires_at=started + timedelta(days=settings.jwt_refresh_ttl_days),
            ip_address=ip_address,
            user_agent=user_agent,
            last_used_at=now,
            family_started_at=started,
        )
        self.db.add(session)
        await self.db.flush()
        access = create_access_token(account.id)
        return TokenResponse(
            access_token=access,
            refresh_token=refresh,
            expires_in=settings.jwt_access_ttl_minutes * 60,
        )

    async def _recent_failures(
        self,
        since: datetime,
        *,
        payload_key: str,
        payload_value: str,
        stop_at_success: bool,
    ) -> list[datetime]:
        """Failed-login timestamps since ``since`` for one lockout dimension, newest first.

        The filter is applied **in SQL**, against the payload field that identifies the
        dimension, so the row cap below bounds one email's or one address's history rather
        than the deployment's. It used to read the newest 500 auth rows across every account
        and sort them out in Python, which broke the control in both directions: a busy
        deployment doing more than 500 sign-ins inside the window silently stopped locking
        anyone out, and an attacker could deliberately produce that state by interleaving
        junk attempts against other addresses to age their own failures out of the window.

        ``stop_at_success`` implements "a successful sign-in clears the budget", and only the
        email dimension asks for it. On the address dimension a success would mean *any* one
        of the people behind a shared address signing in successfully wipes the record of a
        guessing run against everyone else there.
        """
        actions = ["auth_login_failed"]
        if stop_at_success:
            actions.append("auth_login_success")
        result = await self.db.execute(
            select(AuditLog.action, AuditLog.created_at)
            .where(
                AuditLog.created_at >= since,
                AuditLog.action.in_(actions),
                AuditLog.payload[payload_key].as_string() == payload_value,
            )
            .order_by(AuditLog.sequence.desc())
            .limit(_MAX_ATTEMPTS_SCANNED)
        )
        failures: list[datetime] = []
        for action, created_at in result:
            if action == "auth_login_success":
                break
            failures.append(_aware(created_at))
        return failures

    async def _assert_not_locked_out(self, email: str, ip_address: str | None) -> None:
        """Raise TooManyAttemptsError when this email, or this address, has burned its budget.

        Counted from the audit log rather than an in-memory counter so the control holds
        across restarts and across API workers.

        The two dimensions are budgeted **separately**. Pooling them into one counter — which
        is what this did — means failures against one account spend every other account's
        budget on the same address, and behind the reference reverse proxy every clinician on
        the deployment shares one address (see :mod:`app.core.client_address`). Eight typos at
        a shift change therefore locked the entire hospital out of the system for fifteen
        minutes, with the correct password and an untouched account. A credential-guessing
        control that denies sign-in to every clinician on a ward is a worse outcome than the
        guessing it prevents.

        So: the email budget is tight (``login_max_failed_attempts``) because it protects one
        account and only that account's own failures feed it, and the address budget is loose
        (``login_max_failed_attempts_per_ip``) because a legitimate shared egress address
        accumulates other people's typos.
        """
        now = datetime.now(UTC)
        window_start = now - timedelta(minutes=settings.login_attempt_window_minutes)
        needle_email = email.lower()

        if settings.login_max_failed_attempts > 0:
            failures = await self._recent_failures(
                window_start,
                payload_key="email",
                payload_value=needle_email,
                stop_at_success=True,
            )
            await self._assert_within_budget(
                failures,
                budget=settings.login_max_failed_attempts,
                now=now,
                scope="email",
                email=needle_email,
                ip_address=ip_address,
            )

        if ip_address is not None and settings.login_max_failed_attempts_per_ip > 0:
            failures = await self._recent_failures(
                window_start,
                payload_key="ip_address",
                payload_value=ip_address,
                stop_at_success=False,
            )
            await self._assert_within_budget(
                failures,
                budget=settings.login_max_failed_attempts_per_ip,
                now=now,
                scope="ip_address",
                email=needle_email,
                ip_address=ip_address,
            )

    async def _assert_within_budget(
        self,
        failures: list[datetime],
        *,
        budget: int,
        now: datetime,
        scope: str,
        email: str,
        ip_address: str | None,
    ) -> None:
        """Lock out when ``failures`` (newest first) has exhausted ``budget`` and is still hot."""
        if len(failures) < budget:
            return
        # Locked until `login_lockout_minutes` after the most recent failure, so continued
        # hammering keeps extending the lock rather than resetting it.
        if failures[0] + timedelta(minutes=settings.login_lockout_minutes) <= now:
            return
        # Audited (and committed, like a failed login) so lockouts are visible to the
        # security trail rather than only to the caller. `scope` names which of the two
        # budgets tripped, because they call for different responses: an email lockout is a
        # guessing run against one clinician, an address lockout is either a distributed run
        # or a shared egress address that needs its ceiling raised.
        await self.audit.record(
            action="auth_login_locked_out",
            entity_type="account",
            payload={
                "email": email,
                "ip_address": ip_address,
                "failed_attempts": len(failures),
                "scope": scope,
            },
        )
        await self.db.commit()
        raise TooManyAttemptsError(
            "Too many failed sign-in attempts. Try again in "
            f"{settings.login_lockout_minutes} minutes.",
            detail=f"login lockout on {scope}; {len(failures)} failures within the window",
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
        password_ok = await verify_password_async(
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
            raise TokenError(detail="no session row matches the presented refresh token hash")
        if session.is_revoked:
            # Reuse detection. Rotation revokes a refresh token the moment it is exchanged, so
            # a second presentation means the token was captured (or replayed after logout).
            # We cannot tell the attacker's copy from the legitimate one, so revoke every live
            # session for the account and force a fresh sign-in.
            await self._revoke_on_reuse(session)
            # Deliberately the same generic message (and code) as every other refresh failure.
            # Saying "your sessions were signed out because a token was reused" would be kinder
            # to the legitimate clinician but tells a holder of a stolen token that the token was
            # real and already rotated -- i.e. that the victim is active and the copy is worth
            # burning now. The detail below carries that to the log instead. See
            # tests/test_auth_hardening.py::test_every_refresh_failure_is_byte_identical.
            raise TokenError(detail=f"refresh token reuse detected on session {session.id}")
        now = datetime.now(UTC)
        if _aware(session.expires_at) < now:
            # Generic message too, for the same reason as the reuse branch above. This is the
            # check that ends a session at `jwt_refresh_ttl_days` after the *sign-in*, because
            # `expires_at` is anchored to `family_started_at` — see `_issue_tokens`.
            raise TokenError(detail=f"session {session.id} past its absolute expiry")
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
            raise SessionExpiredError(detail=f"session {session.id} idle past the timeout")

        # Rotate: revoke the presented token, issue a new pair. Read before the flush below,
        # because the new row inherits it and the old row is about to stop being the one the
        # family is dated from.
        family_started_at = _aware(session.family_started_at)
        session.is_revoked = True
        await self.db.flush()
        account = await self.db.get(Account, session.account_id)
        if account is None or account.is_deleted:
            raise TokenError(
                "This account is no longer active. Contact your administrator to restore access.",
                detail=f"account {session.account_id} absent or soft-deleted",
            )
        tokens = await self._issue_tokens(
            account,
            ip_address=ip_address,
            user_agent=user_agent,
            family_started_at=family_started_at,
        )
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
            raise NotFoundError(
                "That sign-in session was not found — it may already have been signed out.",
                detail=f"session {session_id} missing or owned by another account",
            )
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
