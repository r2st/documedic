"""Critical/panic lab-value surfacing service.

Wraps app.core.lab_safety with DB access: evaluates a patient's most recent result per marker
against curated critical-value ranges and appends an audit-trail entry for any finding. The
only I/O here is loading LabResult rows and appending to the audit log — no LLM call — so this
stays available even when AI reasoning is degraded/offline (Critical Safety Rule #8).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lab_safety import CriticalLabFlag, evaluate_critical_value
from app.exceptions import PatientNotFoundError
from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.services.audit_service import AuditService


class LabSafetyService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def _patient(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
        result = await self.db.execute(
            select(Patient).where(
                Patient.id == patient_id,
                Patient.account_id == account_id,
                Patient.is_deleted.is_(False),
            )
        )
        patient = result.scalar_one_or_none()
        if patient is None:
            raise PatientNotFoundError()
        return patient

    async def check_patient_labs(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, audit: bool = True
    ) -> list[tuple[LabResult, CriticalLabFlag]]:
        """Evaluate the most recent value per marker for a patient against critical thresholds.

        Does not commit — callers own the transaction boundary (consistent with AuditService).
        """
        await self._patient(account_id, patient_id)
        result = await self.db.execute(
            select(LabResult)
            .where(LabResult.patient_id == patient_id, LabResult.is_deleted.is_(False))
            .order_by(LabResult.marker_name, LabResult.sample_date.desc().nullslast())
        )
        latest_by_marker: dict[str, LabResult] = {}
        for lab in result.scalars().all():
            key = lab.marker_name.strip().lower()
            # Query is ordered by sample_date desc within each marker, so the first row seen
            # per marker is the most recent one.
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
