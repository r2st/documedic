"""Multi-provider encounters: who took part, and the participant's own view of one visit.

Two routers, and the split is the security model rather than tidiness.

``router`` hangs off ``/patients/{patient_id}/encounters/{encounter_id}`` and is the **owner's**
side: only the account that owns the chart may grant, withdraw or list participation. Every one
of its routes resolves the chart first, so it inherits the ownership check every other
patient-scoped route in this API has.

``shared_router`` hangs off ``/encounters/shared`` and is the **participant's** side. It carries
no ``{patient_id}`` at all — deliberately, and this is the whole design. A participant does not
own the chart, so there is no patient id they could supply that would pass the owner's check;
giving them a route that takes one would invite exactly the mistake this feature must not make,
which is letting participation in one visit be widened into access to a record. The encounter id
is the entire authorization subject, and it is resolved against a live participation row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import cast

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clinical import age_from_dob
from app.core.encounter_roles import EncounterRole, may_sign
from app.db.session import get_db
from app.dependencies import get_current_account, rate_limit
from app.models.encounter import Encounter
from app.models.encounter_participant import EncounterParticipant
from app.models.patient import Patient
from app.models.user import Account
from app.openapi import AUTH_ERRORS, PATIENT_ERRORS, errors
from app.schemas.encounter_participant import (
    ParticipantAddRequest,
    ParticipantListResponse,
    ParticipantRemoveRequest,
    ParticipantResponse,
    SharedEncounterListResponse,
    SharedEncounterResponse,
    SharedPatientResponse,
)
from app.services.encounter_participant_service import (
    MAX_SHARED_ROWS,
    EncounterParticipantService,
)
from app.services.encounter_service import EncounterService

router = APIRouter(
    prefix="/patients/{patient_id}/encounters/{encounter_id}/participants",
    tags=["encounters"],
)
shared_router = APIRouter(prefix="/encounters/shared", tags=["encounters"])

# 403 is the shape a refused role takes on both sides. The caller is entitled to the encounter;
# the role they hold or asked for is not one that admits the act.
ROLE_ERRORS = errors(403)


def _participant_response(
    participant: EncounterParticipant, account: Account
) -> ParticipantResponse:
    return ParticipantResponse(
        id=participant.id,
        encounter_id=participant.encounter_id,
        account_id=participant.account_id,
        display_name=account.display_name,
        email=account.email,
        # The column is a plain ``String`` holding one of a closed set; the response narrows it
        # to the enum the client is typed by. The check constraint and the service keep it there.
        role=cast(EncounterRole, participant.role),
        may_sign=may_sign(participant.role),
        purpose=participant.purpose,
        granted_by_account_id=participant.granted_by_account_id,
        created_at=participant.created_at,
        removed_at=participant.removed_at,
        removal_reason=participant.removal_reason,
    )


def _shared_response(
    encounter: Encounter, patient: Patient, participant: EncounterParticipant
) -> SharedEncounterResponse:
    return SharedEncounterResponse(
        id=encounter.id,
        patient=SharedPatientResponse(
            id=patient.id,
            full_name=patient.full_name,
            sex=patient.sex,
            # Derived here rather than shared as a date of birth. Age is what a clinician reads
            # a note against; a DOB is an identifier, and this is a practice that was given one
            # consultation rather than a chart.
            age_years=(
                None
                if patient.date_of_birth is None
                else age_from_dob(patient.date_of_birth, datetime.now(UTC).date())
            ),
        ),
        encounter_date=cast(date, encounter.encounter_date),
        encounter_type=encounter.encounter_type,
        presenting_complaint=encounter.presenting_complaint,
        clinician_notes=encounter.clinician_notes,
        status=encounter.status,
        signed_at=encounter.signed_at,
        signed_by_account_id=encounter.signed_by_account_id,
        amends_encounter_id=encounter.amends_encounter_id,
        amendment_reason=encounter.amendment_reason,
        my_role=cast(EncounterRole, participant.role),
        may_sign=may_sign(participant.role),
        shared_at=participant.created_at,
        purpose=participant.purpose,
    )


# --- The owner's side ---------------------------------------------------------------------


@router.post(
    "",
    response_model=ParticipantResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Share this consultation with a colleague",
    responses=PATIENT_ERRORS | errors(403, 409),
    # Metered because the route takes an email address and answers whether it has an account
    # here. That answer is worth having — see the service — and a ceiling is what keeps it from
    # being an address-enumeration tool. Hourly, and per account: granting access to a
    # consultation is a handful-of-times-a-day act, not a workflow.
    dependencies=[Depends(rate_limit("participant_grant"))],
)
async def add_participant(
    patient_id: uuid.UUID,
    encounter_id: uuid.UUID,
    body: ParticipantAddRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ParticipantResponse:
    """A colleague is granted access to **this one consultation**, in a stated role.

    What they get is the visit — the complaint, the notes, the signature, and enough about the
    patient to know who it is about. Not the chart, not the labs, not the other visits. There
    was previously no such setting: a consultant asked to advise either got the whole panel or
    got nothing, which in practice meant the record was emailed or read out.

    `author` and `supervising` may countersign. Adding either to a note that is already **signed**
    is a **403** — those roles assert that somebody took part in the consultation as it happened,
    and adding one afterwards rewrites who attended an attested visit. `consulting` is still
    accepted on a signed note: a second opinion sought a week later is an ordinary thing to
    record, and it changes no attestation.

    A colleague who already participates is a **409**. Changing somebody's role is a withdrawal
    and a fresh grant, so the record keeps both spells and the dates each applied — an in-place
    edit would leave the chart saying the consultant had been supervising all along.

    `purpose` is required. Audited as `encounter_participant_added`.
    """
    service = EncounterParticipantService(db)
    participant, colleague = await service.add(
        account_id=account.id,
        patient_id=patient_id,
        encounter_id=encounter_id,
        email=str(body.email),
        role=body.role,
        purpose=body.purpose,
    )
    await db.commit()
    return _participant_response(participant, colleague)


@router.get(
    "",
    response_model=ParticipantListResponse,
    summary="Who took part in this consultation",
    responses=PATIENT_ERRORS,
)
async def list_participants(
    patient_id: uuid.UUID,
    encounter_id: uuid.UUID,
    include_removed: bool = Query(
        default=False,
        description=(
            "Include participations that have been withdrawn. Withdrawn rows are kept rather "
            "than deleted, so this is the access history of the consultation — who could see "
            "it, and between which dates."
        ),
    ),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ParticipantListResponse:
    """Oldest first, which reads as the sequence of the consultation.

    Audited as `encounter_participants_viewed`.
    """
    rows = await EncounterParticipantService(db).list_for_encounter(
        account_id=account.id,
        patient_id=patient_id,
        encounter_id=encounter_id,
        include_removed=include_removed,
    )
    await db.commit()
    return ParticipantListResponse(
        encounter_id=encounter_id,
        participants=[_participant_response(p, a) for p, a in rows],
    )


@router.delete(
    "/{participant_id}",
    response_model=ParticipantResponse,
    summary="Withdraw a colleague's access to this consultation",
    responses=PATIENT_ERRORS,
)
async def remove_participant(
    patient_id: uuid.UUID,
    encounter_id: uuid.UUID,
    participant_id: uuid.UUID,
    body: ParticipantRemoveRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ParticipantResponse:
    """Access stops immediately. The row is kept, with the time and the reason.

    Kept rather than deleted because a deleted row answers "who can see this now" and destroys
    "who could see it in March", and the second question is the one an access review asks.

    Withdrawing an already-withdrawn participation is a **404**, not a quiet success: "I have
    just revoked this" and "somebody revoked it in March" must not render the same.

    Audited as `encounter_participant_removed`.
    """
    service = EncounterParticipantService(db)
    participant = await service.remove(
        account_id=account.id,
        patient_id=patient_id,
        encounter_id=encounter_id,
        participant_id=participant_id,
        reason=body.reason,
    )
    colleague = await db.get(Account, participant.account_id)
    await db.commit()
    assert colleague is not None  # FK-guaranteed; the row was just read through its join
    return _participant_response(participant, colleague)


# --- The participant's side ---------------------------------------------------------------


@shared_router.get(
    "",
    response_model=SharedEncounterListResponse,
    summary="Consultations shared with me",
    # No patient in the path and no 404: an account nobody has shared anything with gets an
    # empty list.
    responses=AUTH_ERRORS,
)
async def list_shared_encounters(
    limit: int = Query(
        default=50,
        ge=1,
        le=MAX_SHARED_ROWS,
        description="Most recently shared consultations to return",
    ),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> SharedEncounterListResponse:
    """Most recently granted first. This is a participant's only way in — they cannot list a
    chart they do not own, so without this a share would be a link somebody had to be sent out
    of band, which is the thing this feature exists to stop.

    A consultation whose chart has since been withdrawn disappears from here. A patient who
    withdraws consent withdraws it from the colleague who was shown one visit as much as from
    the practice; a share is not a copy that outlives the record.

    Audited as `shared_encounters_viewed`.
    """
    rows = await EncounterParticipantService(db).list_shared_with(
        account_id=account.id, limit=limit
    )
    await db.commit()
    return SharedEncounterListResponse(
        encounters=[_shared_response(e, p, part) for e, p, part in rows],
        limit=min(limit, MAX_SHARED_ROWS),
    )


@shared_router.get(
    "/{encounter_id}",
    response_model=SharedEncounterResponse,
    summary="One consultation shared with me",
    responses=AUTH_ERRORS | errors(404),
)
async def get_shared_encounter(
    encounter_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> SharedEncounterResponse:
    """The visit, plus the role you hold on it.

    Every way of not being entitled to this is the same **404**: a withdrawn participation, a
    withdrawn chart, an id that never existed, and an id belonging to somebody else's colleague.
    The caller is by construction outside the owning practice, and a 403 would confirm that a
    given encounter id is real.

    Audited as `shared_encounter_viewed`.
    """
    service = EncounterParticipantService(db)
    encounter, patient, participant = await service.shared_encounter(
        account_id=account.id, encounter_id=encounter_id
    )
    await service.audit.record(
        action="shared_encounter_viewed",
        account_id=account.id,
        patient_id=patient.id,
        entity_type="encounter",
        entity_id=encounter.id,
        payload={"role": participant.role},
    )
    await db.commit()
    return _shared_response(encounter, patient, participant)


@shared_router.post(
    "/{encounter_id}/sign",
    response_model=SharedEncounterResponse,
    summary="Countersign a consultation shared with me",
    responses=AUTH_ERRORS | errors(403, 404, 409),
)
async def sign_shared_encounter(
    encounter_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> SharedEncounterResponse:
    """Attest to a visit you took part in. The signature records **your** account.

    This is the point of the roles. A registrar writes the note and a consultant countersigns
    it; before this, `encounters` held one signer and a supervised note was indistinguishable
    from an unsupervised one on the record.

    A role that reads but does not attest — `consulting`, `observing` — is a **403** rather than
    a 404: you have already been shown this encounter, so there is nothing left to conceal, and
    "you may read this and may not sign it" is what lets you do the right thing next.

    Signing an already-signed visit is a **409**, exactly as on the owner's route: a second
    signature would either overwrite the first attestation or record two, with nothing on the
    chart to say which one it means.

    Audited as `encounter_signed`, the same action the owner's signing route writes — it is the
    same clinical act, and splitting it in two would mean an audit query for "who signed this"
    had to know which door it came through.
    """
    service = EncounterParticipantService(db)
    encounter, patient, participant = await service.assert_may_sign(
        account_id=account.id, encounter_id=encounter_id
    )
    signed = await EncounterService(db).sign(account.id, patient.id, encounter.id)
    return _shared_response(signed, patient, participant)
