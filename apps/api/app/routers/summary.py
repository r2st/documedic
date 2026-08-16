"""Clinical handover-summary route.

The one LLM route in this API that produces no ``ClinicalSuggestion`` and assigns no autonomy
tier, because it produces no clinical opinion — see ``app.services.summary_service`` for the
argument, and for the three deterministic controls that hold it to that.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account, rate_limit
from app.models.user import Account
from app.openapi import PATIENT_ERRORS, errors
from app.schemas.summary import ClinicalSummaryResponse
from app.services.audit_service import AuditService
from app.services.patient_service import PatientService
from app.services.summary_service import ClinicalSummaryService

router = APIRouter(prefix="/patients/{patient_id}/summary", tags=["summary"])


@router.get(
    "",
    response_model=ClinicalSummaryResponse,
    summary="A handover summary of this patient's chart",
    responses=PATIENT_ERRORS | errors(429),
    # Metered: this is the only per-patient read in the API that reaches a provider, and it is
    # exactly the shape a ward-round list would fire once per patient per page load.
    dependencies=[Depends(rate_limit("clinical_summary"))],
)
async def get_summary(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ClinicalSummaryResponse:
    """The charted facts, and a written summary of them for a clinician picking this patient up.

    **This is a restatement of the record, not advice about it.** It carries no differential, no
    recommendation and no prognosis, it writes nothing to the chart, and it does not pass through
    the Verifier because it produces nothing for the Verifier to gate. Three things hold that:
    the prompt refuses those tasks outright, every returned string goes through the deterministic
    prescriber-framing control (Critical Safety Rule #4), and the charted facts the summary was
    written from come back in the same response — `chart` before `summary`, so the evidence is
    on screen with the prose (Rule #6).

    Read `source` and `degraded` before trusting the paragraph. `model` means a provider wrote
    it; `deterministic` means none was reachable and the response is the chart restated line by
    line, which is honest but is not a summary; `empty` means there is nothing charted yet. The
    simulated demo provider is deliberately never used here — a fabricated handover reads as a
    statement about this patient.

    `prescriber_framing_applied` is true when the control had to rewrite something the model
    wrote. Nothing is hidden when it fires; the corrected text is what is returned.

    Bounded per section (40 conditions, 40 medications, 30 labs, 10 visits, 30 allergies), most
    recent first. A chart wider than that is summarised from its most recent slice — this is a
    handover, not the export.
    """
    patient = await PatientService(db).get(account.id, patient_id)
    result = await ClinicalSummaryService(db).summarize(patient)
    await AuditService(db).record(
        action="clinical_summary_generated",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        # Counts and provenance, never clinical content: audit_logs.payload is unencrypted,
        # immutable and never pruned, so a condition name written here outlives the record it
        # describes. `source` is the part a DPDP reviewer needs — it says whether this read sent
        # the chart to a third-party provider.
        payload={
            "source": result["source"],
            "degraded": result["degraded"],
            "conditions": len(result["chart"]["active_conditions"]),
            "medications": len(result["chart"]["current_medications"]),
            "labs": len(result["chart"]["recent_labs"]),
            "encounters": len(result["chart"]["recent_encounters"]),
            "prescriber_framing_applied": result["prescriber_framing_applied"],
        },
    )
    await db.commit()
    return ClinicalSummaryResponse.model_validate(result)
