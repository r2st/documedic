"""Clinical protocol templates: the catalogue, the preview, and applying one to a chart."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.order_sets import VERSION, OrderSet, all_order_sets
from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.protocol_application import ProtocolApplication
from app.models.user import Account
from app.openapi import AUTH_ERRORS, PATIENT_ERRORS, errors
from app.schemas.protocol import (
    MAX_APPLICATIONS_ROWS,
    FollowUpResponse,
    InvestigationResponse,
    MedicationResponse,
    OrderSetResponse,
    ProtocolApplicationResponse,
    ProtocolApplyRequest,
    ProtocolFindingResponse,
    ProtocolPreviewResponse,
    ProtocolSelectionRequest,
)
from app.services.audit_service import AuditService
from app.services.protocol_service import ProtocolPreview, ProtocolService

router = APIRouter(prefix="/patients/{patient_id}/order-sets", tags=["protocols"])
# The catalogue is curated reference content shared by every deployment, so it hangs off its
# own prefix rather than under a patient id that would have to be invented to read it.
catalogue_router = APIRouter(prefix="/order-sets", tags=["protocols"])


def _to_template(order_set: OrderSet) -> OrderSetResponse:
    return OrderSetResponse(
        key=order_set.key,
        title=order_set.title,
        condition_name=order_set.condition_name,
        source=order_set.source,
        indication=order_set.indication,
        version=VERSION,
        investigations=[
            InvestigationResponse(
                key=item.key,
                label=item.label,
                marker_name=item.marker_name,
                rationale=item.rationale,
            )
            for item in order_set.investigations
        ],
        medications=[
            MedicationResponse(
                key=item.key,
                generic_name=item.generic_name,
                typical_dose=item.typical_dose,
                dose_unit=item.dose_unit,
                frequency=item.frequency,
                route=item.route,
                note=item.note,
            )
            for item in order_set.medications
        ],
        follow_ups=[
            FollowUpResponse(
                key=item.key,
                label=item.label,
                interval_days=item.interval_days,
                appointment_type=item.appointment_type,
            )
            for item in order_set.follow_ups
        ],
        guideline_section_ids=list(order_set.guideline_section_ids),
    )


def _to_preview(patient_id: uuid.UUID, preview: ProtocolPreview) -> ProtocolPreviewResponse:
    template = _to_template(preview.order_set)
    return ProtocolPreviewResponse(
        patient_id=patient_id,
        template=template,
        selected_keys=list(preview.selected_keys),
        investigations=[
            item for item in template.investigations if item.key in preview.selected_keys
        ],
        medications=[item for item in template.medications if item.key in preview.selected_keys],
        follow_ups=[item for item in template.follow_ups if item.key in preview.selected_keys],
        findings=[
            ProtocolFindingResponse(
                drug=finding.drug_label,
                check_type=finding.flag.check_type,
                severity=finding.flag.severity,
                is_hard_block=finding.flag.is_hard_block,
                summary=finding.flag.summary,
                check_id=finding.check_id,
            )
            for finding in preview.findings
        ],
        is_blocked=preview.is_blocked,
        unresolved_medications=list(preview.unresolved_medications),
    )


def _to_application(row: ProtocolApplication) -> ProtocolApplicationResponse:
    return ProtocolApplicationResponse(
        id=row.id,
        patient_id=row.patient_id,
        template_key=row.template_key,
        template_version=row.template_version,
        template_title=row.template_title,
        selected_keys=list(row.selected_keys or []),
        ordered_investigations=list(row.ordered_investigations or []),
        medication_event_ids=list(row.medication_event_ids or []),
        follow_up_appointment_id=row.follow_up_appointment_id,
        applied_by=row.applied_by,
        warning_count=row.warning_count,
        created_at=row.created_at,
    )


@catalogue_router.get(
    "",
    response_model=list[OrderSetResponse],
    summary="Every curated protocol template",
    responses=AUTH_ERRORS,
)
async def list_templates(
    _account: Account = Depends(get_current_account),
) -> list[OrderSetResponse]:
    """The catalogue: workup, first-line therapy and follow-up interval, per condition.

    Curated reference content compiled into the application, the same material the clinical
    pathways hold. The difference between the two is what they are for — a pathway is read, and
    an order set is *applied*, which is why this one carries item keys and doses.

    Everything here is phrased as a proposal ("guidelines support considering") and never as an
    instruction, and every item is deselectable at apply time. `version` moves whenever the
    curated content changes and is stored on every application, so a correction made later
    cannot rewrite what a clinician ordered earlier.

    Doses are a *typical starting* dose. They exist so the template is usable without a second
    lookup; whether one is right for a given patient is decided by the deterministic dose-range
    check when the set is previewed against that chart.

    No patient data, so this read is not audited as a disclosure.
    """
    return [_to_template(order_set) for order_set in all_order_sets()]


@router.get(
    "/suggested",
    response_model=list[OrderSetResponse],
    summary="Templates matching a condition already on this chart",
    responses=PATIENT_ERRORS,
)
async def suggested_templates(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[OrderSetResponse]:
    """Matched against **documented conditions only** — never against a differential.

    That restriction is the point. Matching on the reasoning engine's hypotheses would put the
    panel's suspicion one click away from being charted as therapy, which is exactly the
    automation bias the rest of this product is built to resist. A condition has to be on the
    chart before an order set for it is offered.

    Suggestion is not endorsement: nothing here says the template is right for this patient,
    only that the chart carries the condition it was written for.

    Discloses which conditions the chart holds, so the read is audited.
    """
    matched = await ProtocolService(db).suggested_for(account_id=account.id, patient_id=patient_id)
    await AuditService(db).record(
        action="protocol_suggestions_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        payload={"matched_templates": [order_set.key for order_set in matched]},
    )
    await db.commit()
    return [_to_template(order_set) for order_set in matched]


@router.post(
    "/{order_set_key}/preview",
    response_model=ProtocolPreviewResponse,
    summary="What this template would chart, and everything standing against it",
    responses=PATIENT_ERRORS,
)
async def preview_template(
    patient_id: uuid.UUID,
    order_set_key: str,
    body: ProtocolSelectionRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ProtocolPreviewResponse:
    """Runs the full deterministic safety engine over the selected medications — plus one check.

    Each medication goes through the same evaluation a single prescription does: allergy,
    interaction, contraindication, renal and hepatic thresholds, dose range against this
    patient's age and weight, duplicate therapy. Every hard block carries the `check_id` an
    override names.

    The extra check is the one a per-drug pass cannot make. Two drugs *in the same template*
    that interact with each other appear in neither one's context, because neither is on the
    chart yet — so a curated set that pairs them would repeat the mistake on every patient it
    is applied to. Those findings come back with no `drug` and never as a hard block: there is
    no persisted row to override, and a block nobody can pass is a dead end rather than a
    control.

    A POST because the selection travels in the body and the evaluation persists the safety
    checks it runs — the same rows `POST ../safety/check` writes, which is what makes each hard
    block overridable.

    Nothing is charted here. `POST ../apply` is what writes, and it recomputes all of this
    first: a preview read at the start of a consultation may be describing a chart that has
    since acquired an allergy.
    """
    preview = await ProtocolService(db).preview(
        account_id=account.id,
        patient_id=patient_id,
        key=order_set_key,
        selected_keys=body.selected_keys,
    )
    await AuditService(db).record(
        action="protocol_previewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        payload={
            "template_key": preview.order_set.key,
            "template_version": preview.version,
            "selected_count": len(preview.selected_keys),
            "finding_count": len(preview.findings),
            "is_blocked": preview.is_blocked,
        },
    )
    await db.commit()
    return _to_preview(patient_id, preview)


@router.post(
    "/{order_set_key}/apply",
    response_model=ProtocolApplicationResponse,
    status_code=201,
    summary="Chart the selected items — all of them, or none",
    responses=PATIENT_ERRORS | errors(409),
)
async def apply_template(
    patient_id: uuid.UUID,
    order_set_key: str,
    body: ProtocolApplyRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ProtocolApplicationResponse:
    """Charts the medications, records the investigations ordered, and books the follow-up.

    **All of it or none of it.** Three things refuse the whole application, with nothing
    written:

    * 409 `hard_block` — a hard block stands against a selected medication. Not dropped from
      the list and applied anyway: half-applying leaves a chart that reads as a completed
      workup with the contraindicated drug quietly missing. Deselect it, or record an override
      with reasoning on the drug-safety screen where that flow already lives.
    * 409 `protocol_template_unusable` — a selected medication cannot be matched to a known
      drug. Nothing in the deterministic engine can evaluate a drug it cannot identify, so
      charting it would put a medication on the record carrying the appearance of having passed
      the same checks as its neighbours.
    * 409 `appointment_conflict` — the follow-up clashes with an existing booking. Same rule: a
      protocol whose drugs were charted and whose review was not is a plan with the recall
      silently missing, and the recall is the part nobody notices is absent.

    The investigations are recorded as **ordered**, not as results. This product has no
    order-entry integration to send a request to, and writing an empty row into the lab results
    would put a test on the chart that reads as one that came back with nothing.

    Omit `follow_up_provider_name` to apply everything else without booking. The follow-up is
    then recorded as selected and nothing goes in the diary.
    """
    application, _preview = await ProtocolService(db).apply(
        account_id=account.id,
        patient_id=patient_id,
        key=order_set_key,
        selected_keys=body.selected_keys,
        applied_by=body.applied_by,
        encounter_id=body.encounter_id,
        follow_up_provider_name=body.follow_up_provider_name,
        follow_up_starts_at=body.follow_up_starts_at,
    )
    await db.commit()
    return _to_application(application)


@router.get(
    "/applications",
    response_model=list[ProtocolApplicationResponse],
    summary="Protocol templates applied to this chart",
    responses=PATIENT_ERRORS,
)
async def list_applications(
    patient_id: uuid.UUID,
    limit: int = Query(
        default=50,
        ge=1,
        le=MAX_APPLICATIONS_ROWS,
        description="Applications to return, most recent first.",
    ),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[ProtocolApplicationResponse]:
    """Which templates were applied, when, by whom, and what each one charted.

    The medication events an application created are ordinary rows on the chart afterwards,
    indistinguishable from ones entered one at a time — so this is the only place the question
    "were these five things one decision?" has an answer. `selected_keys` also records what the
    clinician chose *not* to order, which nothing else on the chart holds.

    Records are append-only: undoing an application means stopping the medications and
    cancelling the appointment, each its own recorded act, not deleting the statement that
    somebody applied it.

    Discloses which protocols a named patient is on, so the read is audited.
    """
    rows = await ProtocolService(db).list_applications(
        account_id=account.id, patient_id=patient_id, limit=limit
    )
    await AuditService(db).record(
        action="protocol_applications_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        payload={"application_count": len(rows)},
    )
    await db.commit()
    return [_to_application(row) for row in rows]
