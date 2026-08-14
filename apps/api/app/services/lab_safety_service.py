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
from sqlalchemy.orm import aliased

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
        # "Most recent per marker" is resolved in SQL by ROW_NUMBER(), not by reading the
        # patient's whole lab history into Python and dropping all but the first row of each
        # run. The old shape was unbounded on the wrong axis: what it transferred and
        # instantiated grew with every result ever ingested, while what it actually evaluates
        # is one row per distinct marker -- a set that stops growing once a patient's panels
        # settle. A chart with 3000 labs across 40 markers built 3000 ORM instances to use 40.
        #
        # The partition key is the *same* normalisation the old dict-dedup applied in Python.
        # It has to be: markers arrive from OCR of whatever format each lab prints, so one
        # patient accumulates "Potassium", "POTASSIUM" and " potassium". When the SQL sort key
        # was the raw ``marker_name`` and only the grouping was case- and whitespace-
        # insensitive, the spellings formed separate runs, the first run's newest row claimed
        # the shared key, and every later spelling was skipped as already seen -- so a panic
        # potassium recorded in a different case than an older normal one was dropped before it
        # ever reached evaluate_critical_value. Doing both in one place makes that class of
        # disagreement unrepresentable.
        #
        # Portability: ROW_NUMBER() OVER (PARTITION BY ...) and NULLS LAST in a window ORDER BY
        # are both plain SQL:2003 that PostgreSQL and SQLite (>= 3.30) each parse natively, so
        # this is one statement on both dialects rather than a dialect-specific DISTINCT ON.
        marker_key = func.lower(func.trim(LabResult.marker_name))
        ranked = (
            select(
                LabResult,
                marker_key.label("marker_key"),
                func.row_number()
                .over(
                    partition_by=marker_key,
                    order_by=(
                        LabResult.sample_date.desc().nullslast(),
                        # Two results for one marker from a single draw is ordinary -- a re-run,
                        # or the same panel entered from two documents. Without a tiebreaker
                        # which of them is "the latest" is whatever the planner returns first,
                        # and on a panic-value screen that is a coin flip between flagging a
                        # critical potassium and not. Most recently recorded wins, and ties on
                        # that resolve by primary key so the answer is at least the same answer
                        # every time.
                        LabResult.created_at.desc(),
                        LabResult.id,
                    ),
                )
                .label("marker_rank"),
            )
            .where(LabResult.patient_id == patient_id, LabResult.is_deleted.is_(False))
            .subquery()
        )
        latest = aliased(LabResult, ranked)
        # Ordering the survivors by the partition key keeps the flag list in a stable, marker-
        # alphabetical order. The old code got that incidentally from its ORDER BY; a window
        # function makes no promise about the order rows leave the outer query in, and the
        # audit payload built below is a record clinicians compare across requests.
        result = await self.db.execute(
            select(latest).where(ranked.c.marker_rank == 1).order_by(ranked.c.marker_key)
        )

        flagged: list[tuple[LabResult, CriticalLabFlag]] = []
        for lab in result.scalars().all():
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
