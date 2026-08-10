"""Clinical-validation harness (Phase 4).

Runs a set of gold-standard vignettes through the real reasoning pipeline and computes the
metrics that underpin the CDSCO SaMD evidence base: top-1/top-3 diagnostic accuracy, can't-miss
recall, hard-block correctness, cited-management coverage, mean citation faithfulness, the
autonomy-tier distribution, and the degraded-mode rate. Each run is persisted as an immutable
``ValidationRun`` and audited.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.reasoning_session import ReasoningSession
from app.models.validation import ValidationRun
from app.services.audit_service import AuditService
from app.services.reasoning_service import ReasoningService

VIGNETTES_PATH = Path(__file__).resolve().parents[4] / "data" / "validation" / "vignettes.json"


def load_vignettes() -> list[dict]:
    return json.loads(VIGNETTES_PATH.read_text())


class ValidationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.reasoning = ReasoningService(db)
        self.audit = AuditService(db)

    async def run(self, account_id: uuid.UUID) -> ValidationRun:
        vignettes = load_vignettes()
        results = [await self._run_vignette(account_id, v) for v in vignettes]
        metrics = self._aggregate(results)

        run = ValidationRun(
            account_id=account_id,
            corpus_version=None,
            vignette_count=len(vignettes),
            metrics=metrics,
            results=results,
            notes="Automated validation harness over gold-standard vignettes.",
        )
        self.db.add(run)
        await self.db.flush()
        await self.audit.record(
            action="validation_run_executed",
            account_id=account_id,
            entity_type="validation_run",
            entity_id=run.id,
            payload={"vignettes": len(vignettes), "metrics": metrics},
        )
        await self.db.commit()
        return run

    async def _run_vignette(self, account_id: uuid.UUID, vignette: dict) -> dict:
        patient = await self._make_patient(account_id, vignette)
        # Start + auto-complete intake (answer every red-flag screen "no").
        session, _ = await self.reasoning.start(
            account_id, patient.id, vignette["presenting_complaint"]
        )
        for _ in range(4):
            pending = await self.reasoning.pending_questions(session.id)
            if not pending:
                break
            await self.reasoning.submit_answers(
                account_id,
                session.id,
                [{"question_id": q.id, "answer_text": "no"} for q in pending],
            )
            session = await self.reasoning.get_session(account_id, session.id)
            if session.intake_complete:
                break

        session, suggestions = await self.reasoning.run(account_id, session.id)
        return self._score(vignette, session, suggestions)

    async def _make_patient(self, account_id: uuid.UUID, vignette: dict) -> Patient:
        patient = Patient(
            account_id=account_id,
            full_name=f"[VALIDATION] {vignette['id']}",
            sex="unknown",
            consent_given=True,
            consent_given_at=datetime.now(UTC),
            notes="Synthetic validation vignette — not a real patient.",
        )
        self.db.add(patient)
        await self.db.flush()
        setup = vignette.get("setup", {})
        for cond in setup.get("conditions", []):
            self.db.add(
                Condition(
                    patient_id=patient.id,
                    condition_name=cond,
                    status="active",
                    clinician_confirmed=True,
                )
            )
        for med in setup.get("medications", []):
            self.db.add(
                MedicationEvent(
                    patient_id=patient.id,
                    generic_name=med,
                    event_type="continue",
                    is_current=True,
                    clinician_confirmed=True,
                )
            )
        for allergen in setup.get("allergies", []):
            self.db.add(
                Allergy(
                    patient_id=patient.id,
                    allergen_name=allergen,
                    allergen_type="drug",
                    status="active",
                    clinician_confirmed=True,
                )
            )
        await self.db.flush()
        return patient

    def _score(self, vignette: dict, session: ReasoningSession, suggestions: list) -> dict:
        diffs = [s for s in suggestions if s.output_type in ("differential", "cant_miss")]
        titles = [s.title.lower() for s in diffs]
        expected = [e.lower() for e in vignette.get("expected_diagnoses", [])]

        top1 = bool(expected and titles and any(e in titles[0] for e in expected))
        top3 = bool(expected and any(e in t for t in titles[:3] for e in expected))

        cant_miss_titles = [s.title.lower() for s in suggestions if s.cant_miss_flag]
        expected_cm = [e.lower() for e in vignette.get("expected_cant_miss", [])]
        cm_recalled = [e for e in expected_cm if any(e in t for t in cant_miss_titles)]

        has_hard_block = any(s.is_hard_block for s in suggestions)
        management = [s for s in suggestions if s.output_type == "management"]
        cited_management = [m for m in management if m.citations]

        return {
            "vignette_id": vignette["id"],
            "title": vignette.get("title"),
            "expected_diagnoses": expected,
            "differential_titles": titles[:5],
            "top1_hit": top1,
            "top3_hit": top3,
            "has_expected_dx": bool(expected),
            "expected_cant_miss": expected_cm,
            "cant_miss_recalled": cm_recalled,
            "expects_hard_block": bool(vignette.get("expects_hard_block")),
            "hard_block_present": has_hard_block,
            "hard_block_correct": bool(vignette.get("expects_hard_block")) == has_hard_block,
            "expects_cited_management": bool(vignette.get("expects_cited_management")),
            "cited_management_present": bool(cited_management),
            "citation_faithfulness": session.case_state.get("citation_faithfulness"),
            "autonomy_tier": session.autonomy_tier,
            "degraded": bool(session.case_state.get("degraded")),
        }

    def _aggregate(self, results: list[dict]) -> dict:
        n = len(results) or 1
        with_dx = [r for r in results if r["has_expected_dx"]]
        cm_expected = sum(len(r["expected_cant_miss"]) for r in results)
        cm_recalled = sum(len(r["cant_miss_recalled"]) for r in results)
        cited_expected = [r for r in results if r["expects_cited_management"]]
        faithfulness = [
            r["citation_faithfulness"] for r in results if r["citation_faithfulness"] is not None
        ]
        tiers: dict[str, int] = {}
        for r in results:
            tiers[r["autonomy_tier"]] = tiers.get(r["autonomy_tier"], 0) + 1

        def ratio(num: int, den: int) -> float:
            return round(num / den, 4) if den else 1.0

        return {
            "vignette_count": len(results),
            "diagnostic_top1_accuracy": ratio(
                sum(1 for r in with_dx if r["top1_hit"]), len(with_dx)
            ),
            "diagnostic_top3_accuracy": ratio(
                sum(1 for r in with_dx if r["top3_hit"]), len(with_dx)
            ),
            "cant_miss_recall": ratio(cm_recalled, cm_expected),
            "hard_block_accuracy": ratio(
                sum(1 for r in results if r["hard_block_correct"]), len(results)
            ),
            "cited_management_rate": ratio(
                sum(1 for r in cited_expected if r["cited_management_present"]),
                len(cited_expected),
            ),
            "mean_citation_faithfulness": round(sum(faithfulness) / len(faithfulness), 4)
            if faithfulness
            else None,
            "autonomy_tier_distribution": tiers,
            "degraded_rate": round(sum(1 for r in results if r["degraded"]) / n, 4),
        }

    async def list_runs(self, account_id: uuid.UUID) -> list[ValidationRun]:
        from sqlalchemy import select

        result = await self.db.execute(
            select(ValidationRun)
            .where(ValidationRun.account_id == account_id)
            .order_by(ValidationRun.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_run(self, account_id: uuid.UUID, run_id: uuid.UUID) -> ValidationRun | None:
        run = await self.db.get(ValidationRun, run_id)
        if run is None or run.account_id != account_id:
            return None
        return run
