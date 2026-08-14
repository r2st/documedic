"""Critical/panic lab-value surfacing service.

Wraps app.core.lab_safety with DB access: evaluates a patient's most recent result per marker
against curated critical-value ranges and appends an audit-trail entry for any finding. The
only I/O here is loading LabResult rows and appending to the audit log — no LLM call — so this
stays available even when AI reasoning is degraded/offline (Critical Safety Rule #8).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lab_safety import CriticalLabFlag, evaluate_critical_value
from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.services.audit_service import AuditService


class LabSafetyService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def _patient(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
        """Fetch a patient the caller owns, or raise PatientNotFoundError.

        Delegates rather than repeating the predicate. This is an authorization check -- it is
        what stops one account reading another's records -- and it was previously copy-pasted
        into four services. Any future change to what "a patient this caller may read" means
        (an extra tenancy dimension, an account-status check) has to land in one place or it
        lands in three and misses the fourth.

        Imported inside the method: PatientService imports from this module's siblings, so a
        module-level import would close a cycle. Same pattern as DocumentService._get_patient.
        """
        from app.services.patient_service import PatientService

        return await PatientService(self.db).get(account_id, patient_id)

    async def check_patient_labs(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, audit: bool = True
    ) -> list[tuple[LabResult, CriticalLabFlag]]:
        """Evaluate the most recent value per marker for a patient against critical thresholds.

        Does not commit — callers own the transaction boundary (consistent with AuditService).
        """
        await self._patient(account_id, patient_id)
        # The sort key must be the *same* key the dedup below groups on. It was previously the
        # raw ``marker_name`` while the grouping was case- and whitespace-insensitive, and the
        # two disagreeing was not cosmetic: markers arrive from OCR of whatever format each
        # lab prints, so one patient accumulates "Potassium", "POTASSIUM" and " potassium".
        # Under a binary collation those sort as three separate runs, the first run's newest
        # row claimed the shared dedup key, and every later spelling was skipped as already
        # seen -- so a panic potassium recorded in a different case than an older normal one
        # was dropped before it ever reached evaluate_critical_value. Normalising in SQL is
        # what keeps "most recent per marker" true across spellings.
        #
        # This costs the read nothing it had: it is a whole-set read that already bitmap-scans
        # and sorts (see ix_lab_results_patient_sample_date), so a functional sort key does not
        # give up an ordered index scan that was being used.
        marker_key = func.lower(func.trim(LabResult.marker_name))
        result = await self.db.execute(
            select(LabResult)
            .where(LabResult.patient_id == patient_id, LabResult.is_deleted.is_(False))
            .order_by(
                marker_key,
                LabResult.sample_date.desc().nullslast(),
                # Two results for one marker from a single draw is ordinary -- a re-run, or the
                # same panel entered from two documents. Without a tiebreaker which of them is
                # "the latest" is whatever the planner returns first, and on a panic-value
                # screen that is a coin flip between flagging a critical potassium and not.
                # Most recently recorded wins, and ties on that resolve by primary key so the
                # answer is at least the same answer every time.
                LabResult.created_at.desc(),
                LabResult.id,
            )
        )
        latest_by_marker: dict[str, LabResult] = {}
        for lab in result.scalars().all():
            key = lab.marker_name.strip().lower()
            # Ordered by that same key, newest first within it, so the first row seen per
            # marker is the most recent one.
            if key not in latest_by_marker:
                latest_by_marker[key] = lab

        flagged: list[tuple[LabResult, CriticalLabFlag]] = []
        for lab in latest_by_marker.values():
            if lab.value_numeric is None:
                continue
            flag = evaluate_critical_value(lab.marker_name, float(lab.value_numeric), lab.unit)
            if flag is not None:
                flagged.append((lab, flag))

        if audit and flagged:
            await self.audit.record(
                action="critical_lab_value_detected",
                account_id=account_id,
                patient_id=patient_id,
                entity_type="lab_result",
                payload={
                    "flags": [
                        {
                            "marker_name": lab.marker_name,
                            "value": flag.value,
                            "unit": flag.unit,
                            "severity": flag.severity,
                        }
                        for lab, flag in flagged
                    ]
                },
            )
        return flagged
