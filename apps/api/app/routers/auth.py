"""Auth routes: signup, login, refresh, logout, me."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.client_address import client_address
from app.core.timing import with_minimum_duration
from app.db.session import get_db
from app.dependencies import get_current_account, rate_limit_by_ip
from app.models.user import Account
from app.openapi import AUTH_ERRORS, errors
from app.schemas.auth import (
    AccountResponse,
    LoginRequest,
    LogoutAllRequest,
    PasswordChangeRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    PasswordResetResponse,
    RefreshRequest,
    SessionResponse,
    SignupRequest,
    TokenResponse,
)
from app.schemas.common import MessageResponse
from app.services.auth_service import AuthService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def deliver_reset_token(token: str | None) -> str | None:
    """Hand a freshly issued reset token to its configured channel; return what the body says.

    ``None`` in — no account, or the account is over its hourly ceiling — is ``None`` out
    through every branch, so no channel can distinguish the cases and neither can the caller.

    ``response`` returns it, which makes the whole flow exercisable against a bare API and is
    refused in production by ``production_config_errors``. ``log`` writes it once, at WARNING
    so it is not filtered out of an operator's view, and returns nothing. There is deliberately
    no third mode that silently drops the token: a reset nobody can complete looks identical
    from the outside to one that worked.
    """
    if token is None:
        return None
    if settings.password_reset_delivery == "response":
        return token
    logger.warning(
        "Password-reset token issued for out-of-band delivery (expires in %d minutes): %s",
        settings.password_reset_token_ttl_minutes,
        token,
    )
    return None


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    """The caller's address and user agent, as recorded on sessions and the auth audit trail.

    ``client_address`` rather than ``request.client.host``: behind the reference reverse proxy
    the peer is nginx, so every clinician's session row and every auth audit entry recorded
    the proxy's address and the access trail could not say where a sign-in came from. See
    :mod:`app.core.client_address`.
    """
    return client_address(request), request.headers.get("user-agent")


@router.post(
    "/signup",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a clinician account and sign in",
    responses=errors(409, 429),
    dependencies=[Depends(rate_limit_by_ip("signup"))],
)
async def signup(
    body: SignupRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    """Register, then immediately issue the same token pair `POST /auth/login` would.

    One round trip, so a new account never lands on a sign-in screen it has just come from.
    A duplicate email is a 409 (`code: email_exists`), not a silent no-op.
    """
    service = AuthService(db)
    await service.signup(body.email, body.password, body.display_name)
    ip, ua = _client_meta(request)
    tokens = await service.login(body.email, body.password, ip_address=ip, user_agent=ua)
    await db.commit()
    return tokens


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange email and password for an access/refresh token pair",
    responses=errors(401, 429),
)
async def login(
    body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    """Sign in. Repeated failures lock the account for a cooldown and return 429.

    A wrong password and an unknown email are indistinguishable — both return 401
    (`invalid_credentials`) — so the endpoint cannot be used to enumerate who has an account.
    """
    service = AuthService(db)
    ip, ua = _client_meta(request)
    tokens = await service.login(body.email, body.password, ip_address=ip, user_agent=ua)
    await db.commit()
    # After the commit, never before: the sweep runs in its own transaction so a failed
    # housekeeping DELETE cannot take the sign-in down with it. See sweep_sessions_if_due.
    await service.sweep_sessions_if_due()
    return tokens


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate a refresh token for a fresh token pair",
    responses=errors(401),
)
async def refresh(
    body: RefreshRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    """Rotating refresh: the submitted token is consumed and a new pair returned.

    Presenting an already-rotated token is treated as theft rather than as a retry — the whole
    token family is revoked, the event is audited, and every device on that family must sign
    in again. Clients must therefore store the newest pair before retrying anything.

    Rotation does not extend the sign-in. Every pair in a family expires
    `JWT_REFRESH_TTL_DAYS` after the password sign-in that began it, however often it has been
    rotated since, so a client that has been refreshing for the whole window gets a 401 and has
    to sign in again rather than a pair it can keep rotating.
    """
    service = AuthService(db)
    ip, ua = _client_meta(request)
    tokens = await service.refresh(body.refresh_token, ip_address=ip, user_agent=ua)
    await db.commit()
    # Rotation is what makes `sessions` grow — one dead row per refresh, per signed-in
    # clinician, forever — so this is the path that most needs the sweep hung off it.
    await service.sweep_sessions_if_due()
    return tokens


@router.post("/logout", response_model=MessageResponse, summary="Revoke one refresh token")
async def logout(body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> MessageResponse:
    """End this device's session. Idempotent: an unknown or already-revoked token still
    reports success, so a client retrying a logout is never left believing it is still signed
    in.

    The access token already issued stops working too, immediately — it names this sign-in in
    its `asid` claim and every authenticated route re-checks that the sign-in is still live.
    This used to say the opposite, and the opposite used to be true: a JWT was good until its
    own expiry, so "log out" left the device reading charts for up to `JWT_ACCESS_TTL_MINUTES`
    more.
    """
    service = AuthService(db)
    await service.logout(body.refresh_token)
    await db.commit()
    return MessageResponse(message="Logged out")


@router.post(
    "/password",
    response_model=MessageResponse,
    summary="Change this account's password",
    responses=errors(401),
)
async def change_password(
    body: PasswordChangeRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Set a new password, re-proving the current one.

    The current password is required even though the caller already holds a valid access
    token: a borrowed unlocked workstation should not be enough to take an account over.

    Every other device is signed out, which is the point of doing this after a suspected
    compromise. Pass `keep_current_refresh_token` to keep the tab you are doing it from signed
    in; omit it to be signed out here as well.
    """
    revoked = await AuthService(db).change_password(
        account,
        body.current_password,
        body.new_password,
        keep_refresh_token=body.keep_current_refresh_token,
    )
    await db.commit()
    return MessageResponse(message=f"Password changed. {revoked} other session(s) were signed out.")


@router.post(
    "/password-reset/request",
    response_model=PasswordResetResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask for a password-reset token",
    responses=errors(429),
    dependencies=[Depends(rate_limit_by_ip("password_reset"))],
)
async def request_password_reset(
    body: PasswordResetRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> PasswordResetResponse:
    """Start a reset. **Always 202, always the same body**, whether or not the address has an
    account and whether or not it has asked too often — otherwise this endpoint answers "who
    works here?" for anyone who cares to ask, which is the question `POST /auth/login` goes to
    some length not to answer.

    The token is single-use and expires in `PASSWORD_RESET_TOKEN_TTL_MINUTES`. Asking again
    invalidates the previous one, so at most one link is ever live.

    How the token reaches the clinician is `PASSWORD_RESET_DELIVERY`. With `response` it comes
    back in `reset_token` below, which makes the flow usable with nothing but this API and is
    refused in production; with `log` it is written to the application log for an operator to
    relay, and `reset_token` is null.

    "Always the same body" was only ever half of it: issuing a token costs a count, an
    invalidation sweep, an insert, an audit row and a commit, and an address with no account
    returned after one indexed SELECT — so the endpoint answered the question in latency that
    its body refuses to answer in words. Both branches are now held to
    `PASSWORD_RESET_MIN_RESPONSE_SECONDS`; see `app.core.timing`.
    """

    async def handle() -> PasswordResetResponse:
        service = AuthService(db)
        token = await service.request_password_reset(body.email, ip_address=client_address(request))
        await db.commit()
        return PasswordResetResponse(
            message=(
                "If that address has an account, a password-reset link is on its way. "
                "The link expires shortly and can be used once."
            ),
            reset_token=deliver_reset_token(token),
        )

    return await with_minimum_duration(
        settings.password_reset_min_response_seconds,
        handle,
        label="POST /auth/password-reset/request",
    )


@router.post(
    "/password-reset/confirm",
    response_model=MessageResponse,
    summary="Spend a reset token and set a new password",
    responses=errors(401),
)
async def confirm_password_reset(
    body: PasswordResetConfirm, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    """Set the password the token's account signs in with, and sign that account out
    everywhere — all sessions, with no exception for the caller, because possession of a reset
    token proves nothing about which existing sessions were theirs.

    Every failure — unknown, expired, spent, superseded — returns the same 401
    (`invalid_reset_token`). Telling them apart would confirm to a holder of someone else's
    token that the address is a live account mid-reset.

    Held to `PASSWORD_RESET_MIN_RESPONSE_SECONDS` for the same reason as the request route, and
    with the branches the other way round: here the *failures* are the fast path — one indexed
    SELECT that finds nothing — while a token that works goes on to hash a password and revoke
    every session. Timing therefore separated "this token is real" from "this token never
    existed", which is the distinction the single shared 401 message is written to withhold.
    """

    async def handle() -> MessageResponse:
        await AuthService(db).reset_password(body.token, body.new_password)
        await db.commit()
        return MessageResponse(
            message="Password reset. Sign in with your new password — all devices were signed out."
        )

    return await with_minimum_duration(
        settings.password_reset_min_response_seconds,
        handle,
        label="POST /auth/password-reset/confirm",
    )


@router.get(
    "/me",
    response_model=AccountResponse,
    summary="The signed-in clinician's own account",
    responses=AUTH_ERRORS,
)
async def me(account: Account = Depends(get_current_account)) -> AccountResponse:
    """Resolve the bearer token to its account. Also the cheapest way for a client to find out
    whether its stored access token is still good.
    """
    return AccountResponse.model_validate(account)


@router.get(
    "/sessions",
    response_model=list[SessionResponse],
    summary="Active sign-in sessions for this account",
    responses=AUTH_ERRORS,
)
async def list_sessions(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[SessionResponse]:
    """Neither revoked nor expired, most recently used first — the "where am I signed in?"
    list. Refresh tokens themselves are stored only as hashes and are never returned.
    """
    sessions = await AuthService(db).list_sessions(account.id)
    return [SessionResponse.model_validate(s) for s in sessions]


@router.delete(
    "/sessions/{session_id}",
    response_model=MessageResponse,
    summary="Sign out one other device",
    responses=errors(401, 404),
)
async def revoke_session(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Revoke a session listed by `GET /auth/sessions`. Only the account's own sessions are
    reachable; another account's id is a 404, not a 403, so the endpoint does not confirm that
    the session exists.
    """
    await AuthService(db).revoke_session(account.id, session_id)
    await db.commit()
    return MessageResponse(message="Session revoked")


@router.post(
    "/logout-all",
    response_model=MessageResponse,
    summary="Sign out everywhere",
    responses=AUTH_ERRORS,
)
async def logout_all(
    body: LogoutAllRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Revoke every active session for this account.

    `keep_current_refresh_token` spares the caller's own session, which is what "sign out my
    other devices" means after a suspected compromise. Omit it to be signed out here too.
    """
    count = await AuthService(db).revoke_all_sessions(
        account.id, keep_refresh_token=body.keep_current_refresh_token
    )
    await db.commit()
    return MessageResponse(message=f"Revoked {count} session(s)")
