"""The patient portal: the clinic's credential management, and the patient's read-only view.

Two routers, and — as with encounter participation — the split *is* the security model.

``router`` hangs off ``/patients/{patient_id}/portal-access`` and is the clinic's side:
issuing, listing and withdrawing credentials. Ordinary clinician authentication, ordinary
ownership check.

``portal_router`` hangs off ``/portal`` and is the patient's. It takes **no patient id in any
path**, and that is the whole design: the chart is named by the credential, so there is no id a
holder could substitute to reach a different record. The commonest way a patient portal leaks is
a route of the form ``/portal/patients/{id}/labs`` where the id is checked against the session —
one forgotten comparison and it is every patient's record. Here there is no comparison to forget,
because there is nothing to compare.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.portal_redaction import grant_is_live
from app.db.session import get_db
from app.dependencies import get_current_account, get_portal_context
from app.models.patient_portal_grant import PatientPortalGrant
from app.models.user import Account
from app.openapi import PATIENT_ERRORS, errors
from app.schemas.patient_portal import (
    PortalAppointmentListResponse,
    PortalAppointmentResponse,
    PortalGrantIssuedResponse,
    PortalGrantListResponse,
    PortalGrantRequest,
    PortalGrantResponse,
    PortalGrantRevokeRequest,
    PortalLabListResponse,
    PortalLabResponse,
    PortalMedicationListResponse,
    PortalMedicationResponse,
    PortalPatientResponse,
)
from app.services.patient_portal_service import PatientPortalService, PortalContext

router = APIRouter(prefix="/patients/{patient_id}/portal-access", tags=["portal"])
portal_router = APIRouter(prefix="/portal", tags=["portal"])

# Every portal read fails the same way — an expired link, a withdrawn one, a withdrawn chart,
# or a string that was never a credential. 401 with one message; see ``PortalAccessError``.
PORTAL_ERRORS = errors(401)


def _grant_response(grant: PatientPortalGrant) -> PortalGrantResponse:
    return PortalGrantResponse(
        id=grant.id,
        patient_id=grant.patient_id,
        issued_by_account_id=grant.issued_by_account_id,
        label=grant.label,
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
        revocation_reason=grant.revocation_reason,
        last_used_at=grant.last_used_at,
        created_at=grant.created_at,
        is_live=grant_is_live(
            expires_at=grant.expires_at,
            revoked_at=grant.revoked_at,
            now=datetime.now(UTC),
        ),
    )


# --- The clinic's side --------------------------------------------------------------------


@router.post(
    "",
    response_model=PortalGrantIssuedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a read-only portal link for this patient",
    responses=PATIENT_ERRORS | errors(409),
)
async def issue_portal_access(
    patient_id: uuid.UUID,
    body: PortalGrantRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PortalGrantIssuedResponse:
    """Mint a credential letting this patient read their own record.

    **The token is in this response and nowhere else, ever.** It is stored only as a SHA-256
    hash, so a database dump is not a set of working credentials to living patients' records —
    and so a lost link is replaced by withdrawing this one and issuing another, not by looking
    the old one up.

    It is an opaque random string rather than a JWT, for two reasons. Revocation has to be
    immediate, and a signed token is valid until it expires whatever the practice does. And a
    third JWT type in this API's vocabulary would be one editing mistake away from a patient's
    credential being accepted on a clinician's route; a string that cannot be decoded at all
    cannot be confused with one that can.

    A chart whose patient has withdrawn consent is refused — issuing portal access against a
    withdrawal would be the clearest possible violation of it.

    Audited as `portal_access_granted`. The token, its hash and the label are all absent from
    that entry.
    """
    issued = await PatientPortalService(db).issue(
        account_id=account.id,
        patient_id=patient_id,
        days_valid=body.days_valid,
        label=body.label,
    )
    await db.commit()
    return PortalGrantIssuedResponse(
        **_grant_response(issued.grant).model_dump(), token=issued.token
    )


@router.get(
    "",
    response_model=PortalGrantListResponse,
    summary="Portal links issued for this chart",
    responses=PATIENT_ERRORS,
)
async def list_portal_access(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PortalGrantListResponse:
    """Newest first, withdrawn credentials included — the access history of the chart.

    `last_used_at` is the field to read: a link issued three months ago and never opened is a
    piece of paper somebody lost, and withdrawing it costs nothing.

    Audited as `portal_access_listed`.
    """
    grants = await PatientPortalService(db).list_grants(
        account_id=account.id, patient_id=patient_id
    )
    await db.commit()
    return PortalGrantListResponse(
        patient_id=patient_id, grants=[_grant_response(grant) for grant in grants]
    )


@router.delete(
    "/{grant_id}",
    response_model=PortalGrantResponse,
    summary="Withdraw a portal link",
    responses=PATIENT_ERRORS,
)
async def revoke_portal_access(
    patient_id: uuid.UUID,
    grant_id: uuid.UUID,
    body: PortalGrantRevokeRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PortalGrantResponse:
    """Stops the link working from the next request. The row is kept, with the time and reason.

    Withdrawing an already-withdrawn link is a **404**, not a quiet success: "I have just
    stopped this" and "this stopped in March" are answers a practice acts on differently, and a
    patient ringing about a lost phone deserves the first one to be true.

    Audited as `portal_access_revoked`.
    """
    grant = await PatientPortalService(db).revoke(
        account_id=account.id,
        patient_id=patient_id,
        grant_id=grant_id,
        reason=body.reason,
    )
    await db.commit()
    return _grant_response(grant)


# --- The patient's side -------------------------------------------------------------------


@portal_router.get(
    "/me",
    response_model=PortalPatientResponse,
    summary="Whose record this link opens",
    responses=PORTAL_ERRORS,
)
async def portal_me(
    context: PortalContext = Depends(get_portal_context),
    db: AsyncSession = Depends(get_db),
) -> PortalPatientResponse:
    """The patient's own name and date of birth, so they can confirm at a glance that this is
    their record and not somebody else's — which is the check that catches a mis-issued
    credential before anything worse does.

    Audited as `portal_record_viewed`.
    """
    service = PatientPortalService(db)
    await service.record_read(context, section="me", items=1)
    await db.commit()
    return PortalPatientResponse(
        full_name=context.patient.full_name,
        date_of_birth=context.patient.date_of_birth,
        sex=context.patient.sex,
    )


@portal_router.get(
    "/labs",
    response_model=PortalLabListResponse,
    summary="Your test results",
    responses=PORTAL_ERRORS,
)
async def portal_labs(
    context: PortalContext = Depends(get_portal_context),
    db: AsyncSession = Depends(get_db),
) -> PortalLabListResponse:
    """Results, newest first — with one deliberate exception.

    **A critical value is not shown until a clinician has acknowledged it.** A potassium of 6.9
    extracted from an overnight report is a result that needs a phone call, not a web page; a
    portal that showed it immediately would tell a patient about a life-threatening value alone,
    at 3am, before anyone clinical had seen it. The entry still appears in the list, marked
    `released: false`, because a portal that silently omitted it would be one nobody could
    trust — and "there is a result your clinical team is reviewing" is the sentence that prompts
    the call.

    Nothing else releases such a result: not time passing, not the chart having been opened.
    Only a clinician recording that they saw it, which is the same act that clears it from the
    critical-lab queue.

    What is here is data — values, units, the reference interval, whether the report marked it
    abnormal. What it *means* is a conversation with a clinician, and no interpretation, no
    differential and no reasoning output reaches this endpoint (Critical Safety Rule #4).

    Audited as `portal_record_viewed`.
    """
    service = PatientPortalService(db)
    decisions = await service.labs_for(context)
    await service.record_read(context, section="labs", items=len(decisions))
    await db.commit()
    return PortalLabListResponse(
        results=[PortalLabResponse.model_validate(d.__dict__) for d in decisions],
        withheld_count=sum(1 for d in decisions if not d.released),
    )


@portal_router.get(
    "/medications",
    response_model=PortalMedicationListResponse,
    summary="Your current medicines",
    responses=PORTAL_ERRORS,
)
async def portal_medications(
    context: PortalContext = Depends(get_portal_context),
    db: AsyncSession = Depends(get_db),
) -> PortalMedicationListResponse:
    """What the record says the patient is currently taking, named as it was prescribed to them.

    Current only. A full prescribing history read without a clinician beside it is a list of
    things somebody might conclude they should restart.

    No drug-safety findings appear here, which is considered rather than overlooked: an
    interaction alert is a prompt for a prescriber to make a decision, and shown to a patient
    with no prescriber attached the reliable outcome is somebody stopping a medicine on their
    own. The checks stay where a clinician can act on them.

    Audited as `portal_record_viewed`.
    """
    service = PatientPortalService(db)
    medications = await service.medications_for(context)
    await service.record_read(context, section="medications", items=len(medications))
    await db.commit()
    return PortalMedicationListResponse(
        medications=[PortalMedicationResponse.model_validate(m.__dict__) for m in medications]
    )


@portal_router.get(
    "/appointments",
    response_model=PortalAppointmentListResponse,
    summary="Your upcoming appointments",
    responses=PORTAL_ERRORS,
)
async def portal_appointments(
    context: PortalContext = Depends(get_portal_context),
    db: AsyncSession = Depends(get_db),
) -> PortalAppointmentListResponse:
    """Upcoming, soonest first. Cancelled and missed bookings are not shown.

    The practice's diary keeps those, and it needs to — a patient who did not attend a follow-up
    is a clinical fact. It reads very differently on the patient's own phone, and "when am I
    next seen" is the question this endpoint exists to answer.

    Audited as `portal_record_viewed`.
    """
    service = PatientPortalService(db)
    appointments = await service.appointments_for(context)
    await service.record_read(context, section="appointments", items=len(appointments))
    await db.commit()
    return PortalAppointmentListResponse(
        appointments=[
            PortalAppointmentResponse(
                id=appointment.id,
                provider_name=appointment.provider_name,
                starts_at=appointment.starts_at,
                ends_at=appointment.ends_at,
                modality=appointment.modality,
                appointment_type=appointment.appointment_type,
            )
            for appointment in appointments
        ]
    )
