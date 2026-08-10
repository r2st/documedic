"""Guideline corpus routes (Phase 3): citation-grounded search and management options."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.schemas.reasoning import CitationOut, ClinicalSuggestionOut
from app.services.guideline_service import GuidelineService
from app.services.reasoning_service import ReasoningService

router = APIRouter(tags=["guidelines"])


@router.get("/guidelines/search", response_model=list[CitationOut])
async def search_guidelines(
    # Bounded so a pathological query cannot drive an unbounded retrieval/embedding cost.
    q: str = Query(..., min_length=2, max_length=500),
    k: int = Query(default=10, ge=1, le=25),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[CitationOut]:
    results = await GuidelineService(db).search(q, k)
    return [
        CitationOut(
            section_id=r["section_id"],
            source=r["source"],
            document_title=r["document_title"],
            heading=r.get("heading"),
            snippet=(r.get("content") or "")[:300],
            score=r.get("score"),
            corpus_version=r.get("corpus_version"),
            page_range=r.get("page_range"),
        )
        for r in results
    ]


@router.get("/guidelines/corpus")
async def corpus_info(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> dict:
    count = await GuidelineService(db).count()
    return {
        "corpus_version": settings.guideline_corpus_version,
        "chunk_count": count,
        "retrieval_threshold": settings.guideline_retrieval_threshold,
        "citation_faithfulness_target": settings.citation_faithfulness_target,
    }


@router.get(
    "/reasoning/{session_id}/management-options",
    response_model=list[ClinicalSuggestionOut],
)
async def management_options(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[ClinicalSuggestionOut]:
    suggestions = await ReasoningService(db).list_suggestions(account.id, session_id)
    return [
        ClinicalSuggestionOut.model_validate(s)
        for s in suggestions
        if s.output_type == "management"
    ]
