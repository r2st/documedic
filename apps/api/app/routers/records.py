"""Longitudinal patient record route."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.openapi import PATIENT_ERRORS
from app.schemas.record import (
    CriticalLabFlagItem,
    CriticalLabFlagsResponse,
    LongitudinalRecord,
    UnreadableLabItem,
)
from app.services.audit_service import AuditService
from app.services.export_service import PatientExportService
from app.services.lab_safety_service import LabSafetyService
from app.services.patient_service import PatientService
from app.services.record_service import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, RecordService

router = APIRouter(prefix="/patients/{patient_id}/record", tags=["records"])
labs_router = APIRouter(prefix="/patients/{patient_id}/labs", tags=["records"])
export_router = APIRouter(prefix="/patients/{patient_id}/export", tags=["records"])


@router.get(
    "",
    response_model=LongitudinalRecord,
    summary="The assembled longitudinal record",
    responses=PATIENT_ERRORS,
)
async def get_record(
    patient_id: uuid.UUID,
    limit: int = Query(
        default=DEFAULT_PAGE_LIMIT,
        ge=1,
        le=MAX_PAGE_LIMIT,
        description="Rows to return **from each section**, not in total.",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Rows to skip in each section. Applied to every section independently.",
    ),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> LongitudinalRecord:
    """Everything approved into the patient graph: medications, labs, conditions, allergies and
    derived markers, in one timeline.

    Encounters are **not** a section here, though this said they were. They are charted (an
    approved extraction merges them) and they are exported, but the chart view has never
    returned them and the five sections below are the five it has — a clinician reading this to
    find out whether a visit is on the record would have been told to look somewhere that never
    had it.

    The widest clinical read in the API, and audited as `patient_record_viewed` for that
    reason. Reads the graph directly — no LLM, so it is unaffected when reasoning is offline.

    **Paged per section.** `limit` and `offset` are applied to each of the five collections
    separately — they are not one sequence and there is no cursor that could walk them as one
    — so a response is a page of medications *and* a page of labs *and* so on, each ordered
    newest-first within its own section. Paging deep into a long section will therefore return
    the short ones empty; that is the shape of the resource, not an error.

    Read `pagination.<section>.has_more` before telling a clinician what is on a chart. A
    section's `total` is the count before paging, so a short page and a last page are
    distinguishable — do not infer either from the length of the array.
    """
    # Ownership check (raises if not found / not owned) plus a PHI-access audit entry: this
    # returns the whole longitudinal record, the widest clinical read in the API.
    await PatientService(db).get(account.id, patient_id)
    record = await RecordService(db).assemble(patient_id, limit=limit, offset=offset)
    await AuditService(db).record(
        action="patient_record_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
    )
    await db.commit()
    return record


@export_router.get(
    "",
    summary="The patient's whole record as a FHIR R4 Bundle",
    responses=PATIENT_ERRORS,
    response_class=JSONResponse,
)
async def export_record(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Everything in the patient graph, as a FHIR R4 `collection` Bundle
    (`application/fhir+json`): demographics, encounters, allergies, conditions, medications,
    lab results and computed markers.

    Findings reference the visit they were recorded at, where the record knows it — so the
    bundle is a chart rather than a pile of resources. The reference is only written when that
    `Encounter` is in the same bundle, so it can never dangle.

    **Not paged.** This is the export, not the chart view — if a section exceeds the per-section
    ceiling the bundle carries an `OperationOutcome` saying which one and that the file is not
    the whole record. A short export that does not admit it is worse than no export.

    Every resource carries whether a clinician confirmed it and which uploaded document it came
    from, so a receiving system can tell a confirmed entry from an unreviewed extraction.

    What is **not** here: differential diagnoses, reasoning sessions and agent output. Those are
    this system's opinions about the patient rather than the patient's record, and a suggestion
    that arrives elsewhere as a plain FHIR `Condition` is indistinguishable from a diagnosis a
    clinician made.

    Audited as `patient_record_exported` — the widest disclosure this API performs.
    """
    patient = await PatientService(db).get(account.id, patient_id)
    bundle = await PatientExportService(db).build_bundle(patient)
    await AuditService(db).record(
        action="patient_record_exported",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        payload={"format": "fhir-r4", "resources": len(bundle["entry"])},
    )
    await db.commit()
    return JSONResponse(
        content=bundle,
        media_type="application/fhir+json",
        headers={
            "Content-Disposition": (f'attachment; filename="aether-record-{patient_id}.fhir.json"')
        },
    )


@labs_router.get(
    "/critical-flags",
    response_model=CriticalLabFlagsResponse,
    summary="Panic/critical values in the patient's latest labs",
    responses=PATIENT_ERRORS,
)
async def critical_lab_flags(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> CriticalLabFlagsResponse:
    """Deterministic, offline panic/critical-value check on the patient's latest labs.

    Re-runs on every call (no caching) so it reflects the current record, and re-audits any
    finding — Critical Safety Rule #8 (must work without an LLM).
    """
    screen = await LabSafetyService(db).screen_labs(account_id=account.id, patient_id=patient_id)
    await db.commit()
    return CriticalLabFlagsResponse(
        patient_id=patient_id,
        flags=[
            CriticalLabFlagItem(
                lab_result_id=lab.id,
                marker_name=flag.marker_name,
                value=flag.value,
                unit=flag.unit,
                severity=flag.severity,
                summary=flag.summary,
                details=flag.details,
            )
            for lab, flag in screen.flags
        ],
        unreadable=[
            UnreadableLabItem(
                lab_result_id=lab.id,
                marker_name=note.marker_name,
                value=note.value,
                unit=note.unit,
                reason=note.reason,
                summary=note.summary,
            )
            for lab, note in screen.unreadable
        ],
    )
