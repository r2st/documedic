"""Reasoning-engine routes: sessions, adaptive intake, pipeline run, SSE theatre, decisions."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Sequence

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import create_stream_token, decode_token
from app.db.session import get_db
from app.dependencies import enforce_rate_limit, get_current_account, rate_limit
from app.exceptions import TokenError
from app.models.intake import IntakeQuestion
from app.models.reasoning_session import ReasoningSession
from app.models.user import Account
from app.openapi import errors
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
from app.services.audit_service import AuditService
from app.services.reasoning_service import ReasoningService

router = APIRouter(tags=["reasoning"])

# Every route here is scoped to one session or one patient, and a session belonging to another
# account is a 404 rather than a 403 — the same rule the patient routes follow.
_SESSION_ERRORS = errors(401, 404)
# The routes that spend an LLM call also advertise the 429 their rate limit can return.
_METERED_SESSION_ERRORS = errors(401, 404, 429)
# Opening a session is the one route here that checks consent, so it is the only one that can
# 403 — the later steps of a session that was lawfully opened stay available. See
# ``ReasoningService.start``.
_START_SESSION_ERRORS = errors(401, 403, 404, 429)


def _intake_state(session: ReasoningSession, pending: Sequence[IntakeQuestion]) -> IntakeStateOut:
    return IntakeStateOut(
        session=ReasoningSessionOut.model_validate(session),
        pending_questions=[IntakeQuestionOut.model_validate(q) for q in pending],
        intake_complete=session.intake_complete,
    )


@router.post(
    "/patients/{patient_id}/reasoning",
    response_model=IntakeStateOut,
    status_code=201,
    summary="Open a reasoning session on a presenting complaint",
    responses=_START_SESSION_ERRORS,
    dependencies=[Depends(rate_limit("reasoning_intake"))],
)
async def start_reasoning(
    patient_id: uuid.UUID,
    body: StartReasoningRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> IntakeStateOut:
    """Start the pipeline at intake and return the Triage agent's first questions.

    Intake comes before reasoning by design: the Triage agent asks rather than guessing at
    what is missing, so `pending_questions` is usually non-empty here and `intake_complete` is
    false. Answer them at `POST /reasoning/{session_id}/intake/answers`, then run the engine.
    """
    service = ReasoningService(db)
    session, questions = await service.start(account.id, patient_id, body.presenting_complaint)
    return _intake_state(session, questions)


@router.get(
    "/reasoning/{session_id}",
    response_model=ReasoningSessionOut,
    summary="A reasoning session's current state",
    responses=_SESSION_ERRORS,
)
async def get_session(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ReasoningSessionOut:
    """Status, presenting complaint, and the accumulated case state.

    `status` moves `created` → `intake` → `intake_complete` → `reasoning` → `completed`, and
    can land on `failed` or `offline_paused` — the latter when no LLM provider is reachable,
    which pauses reasoning without touching the deterministic safety checks.
    """
    session = await ReasoningService(db).get_session(account.id, session_id)
    return ReasoningSessionOut.model_validate(session)


@router.get(
    "/reasoning/{session_id}/intake",
    response_model=list[IntakeQuestionOut],
    summary="Clarifying questions still awaiting an answer",
    responses=_SESSION_ERRORS,
)
async def get_intake(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[IntakeQuestionOut]:
    """Unanswered questions only. Each carries a `question_type` — `red_flag` questions probe
    for time-critical presentations and are the ones worth surfacing first."""
    service = ReasoningService(db)
    await service.get_session(account.id, session_id)
    pending = await service.pending_questions(session_id)
    return [IntakeQuestionOut.model_validate(q) for q in pending]


@router.post(
    "/reasoning/{session_id}/intake/answers",
    response_model=IntakeStateOut,
    summary="Answer clarifying questions",
    responses=_METERED_SESSION_ERRORS,
    dependencies=[Depends(rate_limit("reasoning_intake"))],
)
async def submit_answers(
    session_id: uuid.UUID,
    body: SubmitAnswersRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> IntakeStateOut:
    """Submit answers and get back whatever the Triage agent still wants to know.

    Iterative: answers can raise follow-up questions, so `pending_questions` may be non-empty
    again. When `intake_complete` is true the engine can be run.
    """
    service = ReasoningService(db)
    session, questions = await service.submit_answers(
        account.id, session_id, [a.model_dump() for a in body.answers]
    )
    return _intake_state(session, questions)


@router.post(
    "/reasoning/{session_id}/run",
    response_model=ReasoningResultOut,
    summary="Run the eight-agent pipeline and return its verified output",
    responses=_METERED_SESSION_ERRORS,
    dependencies=[Depends(rate_limit("reasoning_run"))],
)
async def run_reasoning(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ReasoningResultOut:
    """Run the whole panel to completion and return the immutable suggestions it produced.

    The specialist panel reasons independently, the can't-miss sentinel and the devil's
    advocate run regardless of what the panel concluded, and the Verifier gates everything —
    there is no path around it and no flag that skips it. Where agents disagreed, the more
    conservative autonomy tier is the one that survives into the output.

    Suggestions are `ClinicalSuggestion` records: written to the audit trail before they are
    returned, and never subsequently edited or deleted. A correction is a new record pointing
    at the one it supersedes.

    Blocking, and the panel is slow. `GET ../stream` runs the same pipeline over SSE and emits
    each agent's contribution as it lands, which is what the Reasoning Theatre uses.
    """
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
            raise TokenError(detail=f"header token type {payload.get('type')!r}, expected access")
    elif query_token:
        payload = decode_token(query_token)
        if payload.get("type") != "stream":
            raise TokenError(detail=f"query token type {payload.get('type')!r}, expected stream")
        if payload.get("sid") != str(session_id):
            raise TokenError(
                "This reasoning stream could not be reopened. Reload the Reasoning Theatre "
                "to resume watching the agents deliberate.",
                detail=f"stream token bound to session {payload.get('sid')!r}, not {session_id}",
            )
    else:
        raise TokenError(
            "You are not signed in. Sign in to open the Reasoning Theatre.",
            detail="SSE request carried neither a Bearer header nor a ?token= stream token",
        )

    try:
        account_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise TokenError(detail="token subject is missing or not a uuid") from exc
    account = await db.get(Account, account_id)
    if account is None or account.is_deleted:
        raise TokenError(
            "This account is no longer active. Contact your administrator to restore access.",
            detail=f"account {account_id} absent or soft-deleted",
        )
    return account


@router.post(
    "/reasoning/{session_id}/stream-token",
    response_model=StreamTokenOut,
    summary="Mint a short-lived token for the SSE stream",
    responses=_SESSION_ERRORS,
)
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


@router.get(
    "/reasoning/{session_id}/stream",
    summary="Stream the pipeline as it runs (Server-Sent Events)",
    response_class=StreamingResponse,
    responses=_METERED_SESSION_ERRORS
    | {
        200: {
            "description": (
                "An SSE stream. Each agent's contribution arrives as its own named event as "
                "the pipeline produces it, terminated by `event: done`."
            ),
            "content": {"text/event-stream": {}},
        }
    },
)
async def stream_reasoning(
    session_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Run the pipeline and stream Reasoning Theatre events over Server-Sent Events.

    Authenticated by the usual `Authorization: Bearer <access>` header, or — because a
    browser's `EventSource` cannot set headers — by `?token=` carrying a **stream** token from
    `POST ../stream-token`. That token is bound to this one session and expires in minutes,
    which is the point: query strings are recorded verbatim by proxy logs and browser history,
    so what ends up there must grant nothing but this stream.

    Metered against the same per-account budget as `POST ../run` — it is the same eight-agent
    pipeline — and returns 429 with `Retry-After` when that is exhausted. This is the route the
    ceiling exists for: a browser's `EventSource` reconnects automatically on every transport
    error, so a tab left open on a failing network re-runs the whole panel on a loop with nobody
    watching.
    """
    account = await _account_from_query_or_header(request, db, session_id)
    # Enforced here rather than as a route dependency: `rate_limit()` resolves the account from
    # the bearer header, and this route deliberately also accepts a `?token=` stream token that
    # `get_current_account` rejects. Placed after auth so an unauthenticated reconnect cannot
    # spend the account's budget, and before `get_session` so a throttled request costs one
    # dictionary lookup rather than a query.
    enforce_rate_limit("reasoning_run", str(account.id), subject="account")
    service = ReasoningService(db)
    await service.get_session(account.id, session_id)

    async def event_source() -> AsyncGenerator[str, None]:
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


@router.get(
    "/reasoning/{session_id}/suggestions",
    response_model=list[ClinicalSuggestionOut],
    summary="Every clinical suggestion this session produced",
    responses=_SESSION_ERRORS,
)
async def list_suggestions(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[ClinicalSuggestionOut]:
    """The full verified set — differentials, can't-miss diagnoses, investigations, management.

    Includes the dissent. The devil's advocate's counter-argument and the sentinel's
    low-ranked-but-dangerous entries are part of this list by design and must not be filtered
    out, collapsed by default, or sorted below the leading hypothesis in a client.

    This is the engine's clinical output about a named patient, so the read is audited as
    `clinical_suggestions_viewed` against that patient's trail.
    """
    service = ReasoningService(db)
    session = await service.get_session(account.id, session_id)
    suggestions = await service.list_suggestions(account.id, session_id)
    # Recorded against the session's patient, not the session alone — the trail is read per
    # patient, and a disclosure that only names a session id is invisible from that view.
    await AuditService(db).record(
        action="clinical_suggestions_viewed",
        account_id=account.id,
        patient_id=session.patient_id,
        entity_type="reasoning_session",
        entity_id=session_id,
        payload={"suggestion_count": len(suggestions)},
    )
    await db.commit()
    return [ClinicalSuggestionOut.model_validate(s) for s in suggestions]


@router.post(
    "/reasoning/{session_id}/suggestions/{suggestion_id}/decision",
    response_model=DecisionOut,
    status_code=201,
    summary="Record what the clinician did with a suggestion",
    responses=_SESSION_ERRORS,
)
async def record_decision(
    session_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    body: DecisionRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DecisionOut:
    """`acknowledged`, `accepted`, `dismissed` or `overridden`, with an optional reason.

    The clinician is the decision-maker and this is where that is written down. It is also the
    signal the Phase 4 validation harness measures against — an accepted suggestion and a
    dismissed one say very different things about whether the engine is useful.

    The suggestion itself is untouched: this is a separate audited record, not an edit.
    """
    record = await ReasoningService(db).record_decision(
        account.id, session_id, suggestion_id, body.decision.value, body.reason
    )
    return DecisionOut.model_validate(record)
