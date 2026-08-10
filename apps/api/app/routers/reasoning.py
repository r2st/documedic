"""Reasoning-engine routes: sessions, adaptive intake, pipeline run, SSE theatre, decisions."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import create_stream_token, decode_token
from app.db.session import get_db
from app.dependencies import get_current_account
from app.exceptions import TokenError
from app.models.user import Account
from app.schemas.reasoning import (
    ClinicalSuggestionOut,
    DecisionOut,
    DecisionRequest,
    IntakeQuestionOut,
    IntakeStateOut,
    ReasoningResultOut,
    ReasoningSessionOut,
    StartReasoningRequest,
    StreamTokenOut,
    SubmitAnswersRequest,
)
from app.services.reasoning_service import ReasoningService

router = APIRouter(tags=["reasoning"])


def _intake_state(session, pending) -> IntakeStateOut:
    return IntakeStateOut(
        session=ReasoningSessionOut.model_validate(session),
        pending_questions=[IntakeQuestionOut.model_validate(q) for q in pending],
        intake_complete=session.intake_complete,
    )


@router.post("/patients/{patient_id}/reasoning", response_model=IntakeStateOut, status_code=201)
async def start_reasoning(
    patient_id: uuid.UUID,
    body: StartReasoningRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> IntakeStateOut:
    service = ReasoningService(db)
    session, questions = await service.start(account.id, patient_id, body.presenting_complaint)
    return _intake_state(session, questions)


@router.get("/reasoning/{session_id}", response_model=ReasoningSessionOut)
async def get_session(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ReasoningSessionOut:
    session = await ReasoningService(db).get_session(account.id, session_id)
    return ReasoningSessionOut.model_validate(session)


@router.get("/reasoning/{session_id}/intake", response_model=list[IntakeQuestionOut])
async def get_intake(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[IntakeQuestionOut]:
    service = ReasoningService(db)
    await service.get_session(account.id, session_id)
    pending = await service.pending_questions(session_id)
    return [IntakeQuestionOut.model_validate(q) for q in pending]


@router.post("/reasoning/{session_id}/intake/answers", response_model=IntakeStateOut)
async def submit_answers(
    session_id: uuid.UUID,
    body: SubmitAnswersRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> IntakeStateOut:
    service = ReasoningService(db)
    session, questions = await service.submit_answers(
        account.id, session_id, [a.model_dump() for a in body.answers]
    )
    return _intake_state(session, questions)


@router.post("/reasoning/{session_id}/run", response_model=ReasoningResultOut)
async def run_reasoning(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ReasoningResultOut:
    service = ReasoningService(db)
    session, suggestions = await service.run(account.id, session_id)
    return ReasoningResultOut(
        session=ReasoningSessionOut.model_validate(session),
        suggestions=[ClinicalSuggestionOut.model_validate(s) for s in suggestions],
        case_state=session.case_state,
    )


async def _account_from_query_or_header(
    request: Request, db: AsyncSession, session_id: uuid.UUID
) -> Account:
    """SSE auth.

    A normal ``Authorization: Bearer <access>`` header is accepted as usual. Browsers'
    EventSource cannot set headers, so ``?token=`` is also accepted — but only for a
    ``stream``-type token bound to this exact session, minted seconds earlier by
    ``POST /reasoning/{session_id}/stream-token``. A real access token in the query string
    would be recorded verbatim by proxy access logs and browser history.
    """
    auth = request.headers.get("Authorization", "")
    header_token = auth.split(" ", 1)[1].strip() if auth.startswith("Bearer ") else None
    query_token = request.query_params.get("token")

    if header_token:
        payload = decode_token(header_token)
        if payload.get("type") != "access":
            raise TokenError("Wrong token type")
    elif query_token:
        payload = decode_token(query_token)
        if payload.get("type") != "stream":
            raise TokenError("Wrong token type")
        if payload.get("sid") != str(session_id):
            raise TokenError("Stream token is not valid for this session")
    else:
        raise TokenError("Missing access token")

    try:
        account_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise TokenError("Malformed token subject") from exc
    account = await db.get(Account, account_id)
    if account is None or account.is_deleted:
        raise TokenError("Account not found")
    return account


@router.post("/reasoning/{session_id}/stream-token", response_model=StreamTokenOut)
async def mint_stream_token(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> StreamTokenOut:
    """Mint a short-lived token the browser can put in the SSE query string.

    Authenticated with the normal bearer header, and only for a session this account owns,
    so the token that ends up in logs grants nothing but this one stream.
    """
    await ReasoningService(db).get_session(account.id, session_id)
    return StreamTokenOut(
        token=create_stream_token(account.id, session_id),
        expires_in=settings.stream_token_ttl_seconds,
    )


@router.get("/reasoning/{session_id}/stream")
async def stream_reasoning(
    session_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Run the pipeline and stream Reasoning Theatre events over Server-Sent Events."""
    account = await _account_from_query_or_header(request, db, session_id)
    service = ReasoningService(db)
    await service.get_session(account.id, session_id)

    async def event_source():
        yield ": reasoning theatre stream open\n\n"
        async for event, data in service.stream(account.id, session_id):
            yield f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/reasoning/{session_id}/suggestions", response_model=list[ClinicalSuggestionOut])
async def list_suggestions(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[ClinicalSuggestionOut]:
    suggestions = await ReasoningService(db).list_suggestions(account.id, session_id)
    return [ClinicalSuggestionOut.model_validate(s) for s in suggestions]


@router.post(
    "/reasoning/{session_id}/suggestions/{suggestion_id}/decision",
    response_model=DecisionOut,
    status_code=201,
)
async def record_decision(
    session_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    body: DecisionRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DecisionOut:
    record = await ReasoningService(db).record_decision(
        account.id, session_id, suggestion_id, body.decision.value, body.reason
    )
    return DecisionOut.model_validate(record)
