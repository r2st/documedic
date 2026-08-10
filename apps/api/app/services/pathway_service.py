"""Clinical pathway service: assembles ClinicalPathway definitions with live guideline
citations pulled by section_id (never fabricated -- see app.core.pathways docstring)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pathways import ClinicalPathway, available_conditions, get_pathway
from app.exceptions import PathwayNotFoundError, PatientNotFoundError
from app.models.condition import Condition
from app.models.patient import Patient
from app.services.guideline_service import GuidelineService


class PathwayService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.guidelines = GuidelineService(db)

    async def _hydrate(self, pathway: ClinicalPathway) -> dict:
        all_ids = [sid for stage in pathway.stages for sid in stage.guideline_section_ids]
        chunks = await self.guidelines.get_by_section_ids(all_ids)
        citations_by_id = {
            c["section_id"]: {
                "section_id": c["section_id"],
                "source": c["source"],
                "document_title": c["document_title"],
                "heading": c.get("heading"),
                "snippet": (c.get("content") or "")[:300],
                "score": c.get("score"),
                "corpus_version": c.get("corpus_version"),
                "page_range": c.get("page_range"),
            }
            for c in chunks
        }
        return {
            "condition_name": pathway.condition_name,
            "source": pathway.source,
            "autonomy_tier": "informational",
            "stages": [
                {
                    "key": stage.key,
                    "title": stage.title,
                    "items": list(stage.items),
                    "citations": [
                        citations_by_id[sid]
                        for sid in stage.guideline_section_ids
                        if sid in citations_by_id
                    ],
                }
                for stage in pathway.stages
            ],
        }

    async def get(self, condition_name: str) -> dict:
        pathway = get_pathway(condition_name)
        if pathway is None:
            raise PathwayNotFoundError(
                f"No curated clinical pathway for '{condition_name}'. Available: "
                f"{', '.join(available_conditions())}."
            )
        return await self._hydrate(pathway)

    @staticmethod
    def available() -> list[str]:
        return available_conditions()

    async def for_patient(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> dict:
        patient = await self.db.get(Patient, patient_id)
        if patient is None or patient.account_id != account_id or patient.is_deleted:
            raise PatientNotFoundError()

        result = await self.db.execute(
            select(Condition).where(
                Condition.patient_id == patient_id,
                Condition.is_deleted.is_(False),
                Condition.status == "active",
            )
        )
        conditions = result.scalars().all()

        pathways: list[dict] = []
        unmapped: list[str] = []
        seen: set[str] = set()
        for condition in conditions:
            key = condition.condition_name.strip().lower()
            pathway = get_pathway(key)
            if pathway is None:
                unmapped.append(condition.condition_name)
                continue
            if key in seen:
                continue
            seen.add(key)
            pathways.append(await self._hydrate(pathway))

        return {
            "patient_id": patient_id,
            "pathways": pathways,
            "unmapped_conditions": sorted(set(unmapped)),
        }
