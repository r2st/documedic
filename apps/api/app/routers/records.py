"""Longitudinal patient record route."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account, rate_limit
from app.models.user import Account
from app.openapi import PATIENT_ERRORS, errors
from app.schemas.medication_timeline import PrescriptionTimelineResponse, TimelineDrugOut
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
from app.services.prescription_timeline_service import PrescriptionTimelineService, serialise
from app.services.record_pdf import build_record_pdf
from app.services.record_service import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT, RecordService

router = APIRouter(prefix="/patients/{patient_id}/record", tags=["records"])
labs_router = APIRouter(prefix="/patients/{patient_id}/labs", tags=["records"])
export_router = APIRouter(prefix="/patients/{patient_id}/export", tags=["records"])
medications_router = APIRouter(prefix="/patients/{patient_id}/medications", tags=["records"])

# Comfortably longer than the longest supported FHIR type name. The 422 for an unsupported type
# quotes the requested value back, so an unbounded path segment would be reflected into the
# response at whatever size the URL carried it — the same bound `pathways` puts on its own.
MAX_RESOURCE_TYPE_CHARS = 64


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
    responses=PATIENT_ERRORS | errors(429),
    response_class=JSONResponse,
    # Metered, unlike the paged chart read above it. This is the unpaged one — the whole chart
    # in one file, leaving the system — so it is both the most expensive read and the bulk
    # disclosure surface. See `record_export`.
    dependencies=[Depends(rate_limit("record_export"))],
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


@export_router.get(
    "/pdf",
    summary="The patient's whole record as a printable PDF",
    responses=PATIENT_ERRORS | errors(429),
    response_class=Response,
    # The same bucket as the JSON export above, deliberately. The ceiling is on how often a
    # whole chart may be pulled out of the system, and that question does not have a different
    # answer depending on which format it leaves in — metering them separately would double the
    # bulk-retrieval rate available to a stolen token for no clinical reason.
    dependencies=[Depends(rate_limit("record_export"))],
)
async def export_record_pdf(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The same record as `GET ../export`, typeset for a person rather than for a system.

    Allergies first, then conditions, medications, labs, computed markers and visits. Every row
    says whether a clinician confirmed it or whether it is still an unreviewed extraction, and
    a section that hit the export ceiling is named on the page — a printed chart missing its
    oldest labs is indistinguishable from a chart that never had any.

    Carries no differential diagnoses, no management suggestions and no agent output, for the
    same reason the FHIR bundle does not: those are this system's opinions about the patient,
    and on paper nothing marks them apart from what a clinician recorded.

    The document is typeset in a Latin-alphabet font. If any character in the record could not
    be represented, the page says so and points at the JSON export, which is lossless.

    Audited as `patient_record_exported`, alongside the JSON export and against the same limit.
    """
    patient = await PatientService(db).get(account.id, patient_id)
    sections = await PatientExportService(db).load(patient)
    # Off the event loop: laying out and deflating a chart at the per-section ceiling is real
    # CPU, and this process serves every other clinician's chart reads and SSE streams from the
    # same loop. Same idiom as the extraction pipeline in `DocumentService`.
    pdf, lossy = await asyncio.to_thread(build_record_pdf, sections)
    await AuditService(db).record(
        action="patient_record_exported",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        # `characters_dropped` is on the audit row, not only on the page: if a patient later
        # says the copy they were given misspelled their name, the trail has to be able to
        # answer whether this system printed it wrong and knew.
        payload={
            "format": "pdf",
            "bytes": len(pdf),
            "truncated_sections": sections.incomplete,
            "characters_dropped": lossy,
        },
    )
    await db.commit()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="aether-record-{patient_id}.pdf"'},
    )


@export_router.get(
    "/{resource_type}",
    summary="One FHIR R4 resource type from this patient's record",
    responses=PATIENT_ERRORS | errors(429),
    response_class=JSONResponse,
    # The same bucket as the whole-record export, deliberately: the ceiling is on how often a
    # chart may be pulled out of the system, and a caller who can ask for six resource types in
    # six requests must not thereby get six times the bulk-retrieval rate.
    dependencies=[Depends(rate_limit("record_export"))],
)
async def export_resource_type(
    patient_id: uuid.UUID,
    resource_type: str = Path(
        ...,
        # Comfortably longer than the longest supported type name; the 422 quotes the requested
        # value back, so an unbounded segment would be reflected at whatever size the URL had.
        max_length=MAX_RESOURCE_TYPE_CHARS,
        description="A FHIR R4 resource type. Case-insensitive.",
    ),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """One resource type from the chart, as a FHIR R4 `searchset` Bundle
    (`application/fhir+json`).

    For a system that wants part of a record rather than all of it — a registry that holds
    conditions, a nightly observation sync, an ordering module importing prescriptions. The
    alternative was pulling the whole chart and discarding most of it, on the one route in this
    API that is deliberately unpaged.

    Supported: `Patient`, `Encounter`, `AllergyIntolerance`, `Condition`, `MedicationStatement`,
    `MedicationRequest`, `Observation` (which covers both laboratory results and computed
    markers, as R4 intends). Anything else is a 422 — an empty bundle would say this patient has
    no such resources, which is a different and wrong answer to a typo.

    **`MedicationStatement` and `MedicationRequest` are two readings of the same rows**, not two
    sets of facts: the therapy the patient is on, and the prescribing act that was transcribed.
    Ask for whichever your importer needs. The whole-record bundle carries the statement form
    only, so nothing double-counts. Every `MedicationRequest` here is marked
    `reportedBoolean: true` — this system transcribes prescriptions, it does not issue them.

    **Cross-resource references are absent by design.** A `Condition` in the whole-record bundle
    can point at the `Encounter` it was recorded at because both are in that file; here they are
    not, and a reference to a resource the file does not contain is worse than no reference.

    Audited as `patient_record_exported`, against the same ceiling as the full export.
    """
    patient = await PatientService(db).get(account.id, patient_id)
    bundle = await PatientExportService(db).build_resource_bundle(patient, resource_type)
    await AuditService(db).record(
        action="patient_record_exported",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        payload={
            "format": "fhir-r4",
            # Which slice left the system, so the trail distinguishes a whole-chart disclosure
            # from a single-type one rather than recording both as "exported".
            "resource_type": resource_type,
            "resources": bundle["total"],
        },
    )
    await db.commit()
    return JSONResponse(
        content=bundle,
        media_type="application/fhir+json",
    )


@medications_router.get(
    "/timeline",
    response_model=PrescriptionTimelineResponse,
    summary="This patient's prescribing history, grouped by drug",
    responses=PATIENT_ERRORS,
)
async def prescription_timeline(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PrescriptionTimelineResponse:
    """Every medication event on this chart, assembled per drug instead of per row.

    `GET ../record` returns the medication log the way it is stored — every drug's events
    interleaved by date — so answering "when did this start, what has the dose been, and who
    changed it" meant reconstructing each drug's story by eye. This returns it already
    reconstructed: one group per drug, oldest event first within the group, each entry carrying
    the dose before it (`previous_dose_text`) when the dose changed, and the visit it was
    recorded at where the record links one.

    Groups are ordered current therapy first, then most recently touched. `started_on`,
    `stopped_on` and `is_current` are computed across the whole group — a `stop` event ends the
    therapy whatever an earlier row's flag says, which is the precedence the chart itself uses.

    **Two drugs are only merged when the record proves they are the same drug**: by
    `drug_vocabulary_id` where the row resolved, by normalised name where it did not, and never
    across the two. An unresolved row is flagged `unresolved: true` — no interaction,
    contraindication or allergy rule ever ran for it — rather than being folded into a
    same-looking resolved group.

    This describes prescribing, not taking. Nothing here is an adherence judgement: the record
    holds what was prescribed, and the distance between that and what a patient swallowed is
    exactly where a confident inference would be wrong.

    Deterministic and offline. Audited as `prescription_timeline_viewed`.
    """
    await PatientService(db).get(account.id, patient_id)
    groups = await PrescriptionTimelineService(db).timeline(patient_id)
    await AuditService(db).record(
        action="prescription_timeline_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        payload={
            "drugs": len(groups),
            "events": sum(len(group.entries) for group in groups),
        },
    )
    await db.commit()
    return PrescriptionTimelineResponse(
        patient_id=patient_id,
        medications=[TimelineDrugOut.model_validate(row) for row in serialise(groups)],
        total_drugs=len(groups),
        total_events=sum(len(group.entries) for group in groups),
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
