"""Auth routes: signup, login, refresh, logout, me."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
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


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignupRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    service = AuthService(db)
    await service.signup(body.email, body.password, body.display_name)
    ip, ua = _client_meta(request)
    tokens = await service.login(body.email, body.password, ip_address=ip, user_agent=ua)
    await db.commit()
    return tokens


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    service = AuthService(db)
    ip, ua = _client_meta(request)
    tokens = await service.login(body.email, body.password, ip_address=ip, user_agent=ua)
    await db.commit()
    return tokens


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    service = AuthService(db)
    ip, ua = _client_meta(request)
    tokens = await service.refresh(body.refresh_token, ip_address=ip, user_agent=ua)
    await db.commit()
    return tokens


@router.post("/logout", response_model=MessageResponse)
async def logout(body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> MessageResponse:
    service = AuthService(db)
    await service.logout(body.refresh_token)
    await db.commit()
    return MessageResponse(message="Logged out")


@router.get("/me", response_model=AccountResponse)
async def me(account: Account = Depends(get_current_account)) -> AccountResponse:
    return AccountResponse.model_validate(account)


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[SessionResponse]:
    sessions = await AuthService(db).list_sessions(account.id)
    return [SessionResponse.model_validate(s) for s in sessions]


@router.delete("/sessions/{session_id}", response_model=MessageResponse)
async def revoke_session(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    await AuthService(db).revoke_session(account.id, session_id)
    await db.commit()
    return MessageResponse(message="Session revoked")


@router.post("/logout-all", response_model=MessageResponse)
async def logout_all(
    body: LogoutAllRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    count = await AuthService(db).revoke_all_sessions(
        account.id, keep_refresh_token=body.keep_current_refresh_token
    )
    await db.commit()
    return MessageResponse(message=f"Revoked {count} session(s)")
