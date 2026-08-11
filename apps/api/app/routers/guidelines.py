"""Guideline corpus routes (Phase 3): citation-grounded search and management options."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.openapi import AUTH_ERRORS, errors
from app.schemas.reasoning import CitationOut, ClinicalSuggestionOut
from app.services.audit_service import AuditService
from app.services.guideline_service import GuidelineService
from app.services.reasoning_service import ReasoningService

router = APIRouter(tags=["guidelines"])


@router.get(
    "/guidelines/search",
    response_model=list[CitationOut],
    summary="Search the curated guideline corpus",
    responses=AUTH_ERRORS,
)
async def search_guidelines(
    # Bounded so a pathological query cannot drive an unbounded retrieval/embedding cost.
    q: str = Query(
        ...,
        min_length=2,
        max_length=500,
        description="Free-text clinical query, e.g. `metformin in renal impairment`.",
    ),
    k: int = Query(default=10, ge=1, le=25, description="Maximum passages to return."),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[CitationOut]:
    """Retrieval over ICMR/WHO/NICE, returning passages with their citations.

    Nothing here is generated. Each result names its source document, section and page range,
    so anything quoted from it can be traced back to the guideline it came from — and an empty
    list means the corpus has no answer, not that one should be invented.
    """
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


@router.get(
    "/guidelines/corpus",
    summary="Which guideline corpus is loaded",
    responses=AUTH_ERRORS,
)
async def corpus_info(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Corpus version, chunk count and retrieval thresholds.

    `corpus_version` is what a citation is relative to — a suggestion cited months ago was
    grounded in whatever version was loaded then, which is why it is recorded rather than
    assumed.
    """
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
    summary="Guideline-cited management options from a reasoning session",
    responses=errors(401, 404),
)
async def management_options(
    session_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[ClinicalSuggestionOut]:
    """The `management` subset of the session's verified suggestions.

    Options with citations, carrying an autonomy tier — never instructions. The clinician
    prescribes; this endpoint reports what the guidelines support considering.

    A filtered view of the same clinical output as `GET ../suggestions`, and audited the same
    way (`clinical_suggestions_viewed`, with the subset named in the payload). Auditing one
    route and not the other would leave the disclosure reachable without a record of it.
    """
    service = ReasoningService(db)
    session = await service.get_session(account.id, session_id)
    suggestions = [
        s
        for s in await service.list_suggestions(account.id, session_id)
        if s.output_type == "management"
    ]
    await AuditService(db).record(
        action="clinical_suggestions_viewed",
        account_id=account.id,
        patient_id=session.patient_id,
        entity_type="reasoning_session",
        entity_id=session_id,
        payload={"subset": "management", "suggestion_count": len(suggestions)},
    )
    await db.commit()
    return [ClinicalSuggestionOut.model_validate(s) for s in suggestions]
