"""SBAR handover routes."""

from __future__ import annotations

import uuid
from typing import cast

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.handoff import PatientHandoff
from app.models.user import Account
from app.openapi import PATIENT_ERRORS, errors
from app.schemas.handoff import (
    ChecklistItemResponse,
    HandoffAcknowledgeRequest,
    HandoffChecklistResponse,
    HandoffCreateRequest,
    HandoffResponse,
    HandoffSendRequest,
    HandoffStatus,
    HandoffUpdateRequest,
)
from app.services.audit_service import AuditService
from app.services.handoff_service import HandoffService

router = APIRouter(prefix="/patients/{patient_id}/handoffs", tags=["handoffs"])


def _to_response(handoff: PatientHandoff) -> HandoffResponse:
    return HandoffResponse(
        id=handoff.id,
        patient_id=handoff.patient_id,
        # The column is a plain ``String`` holding one of the three lifecycle values; the
        # response narrows it to the Literal the client is typed by. Both the DB check
        # constraint and ``HandoffService`` keep the column inside that set.
        status=cast(HandoffStatus, handoff.status),
        situation=handoff.situation,
        background=handoff.background,
        assessment=handoff.assessment,
        recommendation=handoff.recommendation,
        from_clinician=handoff.from_clinician,
        to_clinician=handoff.to_clinician,
        checklist=[ChecklistItemResponse(**item) for item in handoff.checklist or []],
        sent_at=handoff.sent_at,
        acknowledged_at=handoff.acknowledged_at,
        acknowledged_by=handoff.acknowledged_by,
        acknowledgement_note=handoff.acknowledgement_note,
        created_at=handoff.created_at,
    )


@router.get(
    "/checklist",
    response_model=HandoffChecklistResponse,
    summary="What this chart is currently carrying that a handover must pass on",
    responses=PATIENT_ERRORS,
)
async def handoff_checklist(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> HandoffChecklistResponse:
    """The chart's outstanding risks, computed — not a list the clinician types.

    A free-form handover checklist is a list of the things the outgoing clinician remembered,
    which is the faculty handover is failing. What is here is derived from the record:
    unacknowledged critical lab values, safety hard blocks standing against currently charted
    drugs, documented allergies, live medications, documents that still need checking against
    the original.

    **Only non-zero items appear.** A checklist that always shows the same five rows, three of
    them permanently "nothing to do", is one people learn to tick without reading — and then the
    two that mattered get ticked the same way.

    Every key returned here must be passed to `POST ../{id}/send` as
    `confirmed_checklist_keys`, so read this immediately before sending: the server recomputes
    it and refuses a send whose confirmation no longer matches the chart.

    Deterministic and offline-capable — five counts and the deterministic safety engine, no LLM.
    Handover happens at shift change, which is exactly when a degraded provider must not take a
    clinical workflow down with it.
    """
    items = await HandoffService(db).build_checklist(account_id=account.id, patient_id=patient_id)
    await AuditService(db).record(
        action="handoff_checklist_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        # Counts of what the chart holds. A disclosure in the sense this trail means — it says
        # how many allergies and live medications a named patient has — so it is recorded.
        payload={item.key: item.count for item in items},
    )
    await db.commit()
    return HandoffChecklistResponse(
        patient_id=patient_id,
        items=[ChecklistItemResponse(**item.as_dict()) for item in items],
    )


@router.post(
    "",
    response_model=HandoffResponse,
    status_code=201,
    summary="Draft an SBAR handover",
    responses=PATIENT_ERRORS,
)
async def create_handoff(
    patient_id: uuid.UUID,
    body: HandoffCreateRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> HandoffResponse:
    """Situation, Background, Assessment, Recommendation — all four required.

    That is the structure's whole purpose: unstructured verbal handover reliably drops the last
    two, and what survives is a name and a diagnosis. A draft is editable and has been handed to
    nobody; `POST ../{id}/send` is what hands it over and freezes it.
    """
    handoff = await HandoffService(db).create(
        account_id=account.id,
        patient_id=patient_id,
        situation=body.situation,
        background=body.background,
        assessment=body.assessment,
        recommendation=body.recommendation,
    )
    await db.commit()
    return _to_response(handoff)


@router.get(
    "",
    response_model=list[HandoffResponse],
    summary="Every handover recorded on this chart",
    responses=PATIENT_ERRORS,
)
async def list_handoffs(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[HandoffResponse]:
    """Newest first. Sent handovers are immutable; drafts are still editable by their author.

    Returns the SBAR prose, which is clinical content about this patient, so the read is audited
    as `handoff_list_viewed`.
    """
    rows = await HandoffService(db).list_for_patient(account_id=account.id, patient_id=patient_id)
    await AuditService(db).record(
        action="handoff_list_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        payload={"handoff_count": len(rows)},
    )
    await db.commit()
    return [_to_response(row) for row in rows]


@router.patch(
    "/{handoff_id}",
    response_model=HandoffResponse,
    summary="Edit a draft handover",
    responses=PATIENT_ERRORS | errors(409),
)
async def update_handoff(
    patient_id: uuid.UUID,
    handoff_id: uuid.UUID,
    body: HandoffUpdateRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> HandoffResponse:
    """409 `handoff_sent` once it has been sent. A correction to a sent handover is a new one.

    Sending is the point at which a clinician asserted "this is what I am handing over", and a
    handover note that can be rewritten afterwards is not a record of what was handed over —
    the same reasoning that freezes a signed encounter.
    """
    handoff = await HandoffService(db).update(
        account_id=account.id,
        patient_id=patient_id,
        handoff_id=handoff_id,
        situation=body.situation,
        background=body.background,
        assessment=body.assessment,
        recommendation=body.recommendation,
    )
    await db.commit()
    return _to_response(handoff)


@router.post(
    "/{handoff_id}/send",
    response_model=HandoffResponse,
    summary="Hand over: freeze the note and snapshot the checklist",
    responses=PATIENT_ERRORS | errors(409),
)
async def send_handoff(
    patient_id: uuid.UUID,
    handoff_id: uuid.UUID,
    body: HandoffSendRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> HandoffResponse:
    """Names both clinicians, confirms the chart's outstanding risks, and freezes the content.

    `confirmed_checklist_keys` must equal the keys `GET ../checklist` returns **at this moment**,
    exactly — not a superset and not a subset. The server recomputes the checklist here and
    refuses a mismatch with 409 `handoff_checklist_stale`.

    That refusal is the point of the feature. A handover drafted at 6pm and sent at 8pm may be
    describing a chart that has since acquired a panic potassium, and a confirmation of a state
    that no longer holds is worse than no confirmation at all: it is a signed assertion that
    somebody reviewed something they never saw. Re-read the checklist and send again.

    Both clinicians are recorded **by name**, not by account. A patient belongs to one account,
    so a handoff addressed to a different account would be addressed to somebody who cannot open
    the chart it is about; and within an account the deployment model is one practice login held
    signed in across a shift, so the id says which practice rather than which person is going
    off shift.
    """
    handoff = await HandoffService(db).send(
        account_id=account.id,
        patient_id=patient_id,
        handoff_id=handoff_id,
        from_clinician=body.from_clinician,
        to_clinician=body.to_clinician,
        confirmed_checklist_keys=body.confirmed_checklist_keys,
    )
    await db.commit()
    return _to_response(handoff)


@router.post(
    "/{handoff_id}/acknowledge",
    response_model=HandoffResponse,
    summary="The receiving clinician's receipt",
    responses=PATIENT_ERRORS | errors(409),
)
async def acknowledge_handoff(
    patient_id: uuid.UUID,
    handoff_id: uuid.UUID,
    body: HandoffAcknowledgeRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> HandoffResponse:
    """Closes the loop that verbal handover leaves open: somebody has taken this patient on.

    409 `handoff_not_sent` for a draft — a receipt for something still being written would
    record that it was received and then freeze it mid-sentence. 409 `handoff_sent` for one
    already acknowledged: a handover is from one named person to one named person, and a second
    receipt would leave the chart unable to say who took the patient on.

    `acknowledged_by` is the receiving clinician's own name. The account this arrives through is
    recorded separately as provenance, and is deliberately not treated as the answer to "who".
    """
    handoff = await HandoffService(db).acknowledge(
        account_id=account.id,
        patient_id=patient_id,
        handoff_id=handoff_id,
        acknowledged_by=body.acknowledged_by,
        note=body.note,
    )
    await db.commit()
    return _to_response(handoff)
