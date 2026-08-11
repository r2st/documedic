"""Auth routes: signup, login, refresh, logout, me."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.openapi import AUTH_ERRORS, errors
from app.schemas.auth import (
    AccountResponse,
    LoginRequest,
    LogoutAllRequest,
    RefreshRequest,
    SessionResponse,
    SignupRequest,
    TokenResponse,
)
from app.schemas.common import MessageResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    return ip, request.headers.get("user-agent")


@router.post(
    "/signup",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a clinician account and sign in",
    responses=errors(409),
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
    """
    service = AuthService(db)
    ip, ua = _client_meta(request)
    tokens = await service.refresh(body.refresh_token, ip_address=ip, user_agent=ua)
    await db.commit()
    return tokens


@router.post("/logout", response_model=MessageResponse, summary="Revoke one refresh token")
async def logout(body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> MessageResponse:
    """End this device's session. Idempotent: an unknown or already-revoked token still
    reports success, so a client retrying a logout is never left believing it is still signed
    in. Access tokens already issued stay valid until they expire.
    """
    service = AuthService(db)
    await service.logout(body.refresh_token)
    await db.commit()
    return MessageResponse(message="Logged out")


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
