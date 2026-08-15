"""Safety-reporting register (Phase 4) — append-only adverse-event capture for the pilot."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import NotFoundError
from app.models.patient import Patient
from app.models.reasoning_session import ReasoningSession
from app.models.validation import SafetyReport
from app.services.audit_service import AuditService


class SafetyReportService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def _own_patient(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> None:
        """Refuse a report linked to a chart this account does not hold.

        ``patient_id`` and ``session_id`` arrive in the request body and were written straight
        onto the row, which made this the one authenticated route in the API that took a
        patient id from the caller without asking whether the caller had that patient. It was
        not a read — nothing about the chart comes back — but it wrote: ``file_report`` audits
        as ``safety_report_filed`` *against the named patient*, so any account could append an
        entry to any chart's trail. That trail is append-only, hash-chained and never pruned,
        and its owner reads it at ``GET /patients/{id}/audit``; an entry attributed to a
        clinician who has nothing to do with the patient cannot be removed and cannot be
        explained. A malformed id was worse in a duller way — both columns are foreign keys, so
        it reached ``flush`` as an IntegrityError and 500ed.

        Withdrawn charts are deliberately still linkable (``is_deleted`` is not filtered here):
        a near-miss on a chart that has since been withdrawn is exactly what post-market
        surveillance exists to capture, and the report is about what the system did, not a way
        back into the record. Ownership is unchanged — another account's chart is a 404.
        """
        row = await self.db.execute(
            select(Patient.id).where(Patient.id == patient_id, Patient.account_id == account_id)
        )
        if row.scalars().first() is None:
            raise NotFoundError(
                "That patient was not found, so the safety report was not filed. File it "
                "without a patient link, or check the chart is one you have open.",
                detail=f"patient {patient_id} absent or owned by another account",
            )

    async def _own_session(self, account_id: uuid.UUID, session_id: uuid.UUID) -> None:
        """The same check for the reasoning session a report names. See :meth:`_own_patient`."""
        row = await self.db.execute(
            select(ReasoningSession.id).where(
                ReasoningSession.id == session_id,
                ReasoningSession.account_id == account_id,
            )
        )
        if row.scalars().first() is None:
            raise NotFoundError(
                "That reasoning session was not found, so the safety report was not filed. "
                "File it without a session link, or check the session is one you ran.",
                detail=f"reasoning session {session_id} absent or owned by another account",
            )

    async def file_report(
        self,
        *,
        account_id: uuid.UUID,
        category: str,
        severity: str,
        description: str,
        patient_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
        detail: dict | None = None,
    ) -> SafetyReport:
        if patient_id is not None:
            await self._own_patient(account_id, patient_id)
        if session_id is not None:
            await self._own_session(account_id, session_id)
        report = SafetyReport(
            account_id=account_id,
            patient_id=patient_id,
            session_id=session_id,
            category=category,
            severity=severity,
            status="open",
            description=description,
            detail=detail or {},
        )
        self.db.add(report)
        await self.db.flush()
        await self.audit.record(
            action="safety_report_filed",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="safety_report",
            entity_id=report.id,
            payload={"category": category, "severity": severity},
        )
        await self.db.commit()
        return report

    async def list_reports(self, account_id: uuid.UUID) -> list[SafetyReport]:
        result = await self.db.execute(
            select(SafetyReport)
            .where(SafetyReport.account_id == account_id)
            .order_by(SafetyReport.created_at.desc())
        )
        return list(result.scalars().all())
