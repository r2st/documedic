"""Performance-metrics aggregation (Phase 4) — the monitored-pilot dashboard backend.

Aggregates operational and safety signals across an account's reasoning sessions and the
immutable suggestion/audit records: session throughput, autonomy-tier distribution, hard-block
and can't-miss counts, verifier-disagreement rate, mean citation faithfulness, degraded-mode
rate, and the open safety-report tally.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.clinical_suggestion import ClinicalSuggestion
from app.models.reasoning_session import ReasoningSession
from app.models.validation import SafetyReport


class MetricsService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def performance(self, account_id: uuid.UUID) -> dict:
        sessions = list(
            (
                await self.db.execute(
                    select(ReasoningSession).where(
                        ReasoningSession.account_id == account_id,
                        ReasoningSession.is_deleted.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        completed = [s for s in sessions if s.status in ("completed", "awaiting_review")]

        tiers: dict[str, int] = {}
        faithfulness: list[float] = []
        disagreements = 0
        degraded = 0
        for s in completed:
            if s.autonomy_tier:
                tiers[s.autonomy_tier] = tiers.get(s.autonomy_tier, 0) + 1
            cf = s.case_state.get("citation_faithfulness")
            if cf is not None:
                faithfulness.append(float(cf))
            if s.case_state.get("verifier_status", "agree") != "agree":
                disagreements += 1
            if s.case_state.get("degraded"):
                degraded += 1

        hard_blocks = await self.db.scalar(
            select(func.count())
            .select_from(ClinicalSuggestion)
            .where(
                ClinicalSuggestion.patient_id.in_(
                    select(ReasoningSession.patient_id).where(
                        ReasoningSession.account_id == account_id
                    )
                ),
                ClinicalSuggestion.is_hard_block.is_(True),
            )
        )
        cant_miss = await self.db.scalar(
            select(func.count())
            .select_from(ClinicalSuggestion)
            .where(
                ClinicalSuggestion.session_id.in_(
                    select(ReasoningSession.id).where(ReasoningSession.account_id == account_id)
                ),
                ClinicalSuggestion.cant_miss_flag.is_(True),
            )
        )

        open_reports = await self.db.scalar(
            select(func.count())
            .select_from(SafetyReport)
            .where(SafetyReport.account_id == account_id, SafetyReport.status != "closed")
        )

        n = len(completed) or 1
        return {
            "pilot_mode": settings.pilot_mode,
            "total_sessions": len(sessions),
            "completed_sessions": len(completed),
            "awaiting_review": sum(1 for s in sessions if s.status == "awaiting_review"),
            "autonomy_tier_distribution": tiers,
            "hard_blocks_total": int(hard_blocks or 0),
            "cant_miss_total": int(cant_miss or 0),
            "verifier_disagreement_rate": round(disagreements / n, 4),
            "degraded_rate": round(degraded / n, 4),
            "mean_citation_faithfulness": round(sum(faithfulness) / len(faithfulness), 4)
            if faithfulness
            else None,
            "citation_faithfulness_target": settings.citation_faithfulness_target,
            "open_safety_reports": int(open_reports or 0),
        }
