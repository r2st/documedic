"""Guideline retrieval service (Phase 3, used by the Guideline-RAG agent in Phase 2+).

Production retrieval is dense vector search in Qdrant; ``qdrant-client`` and the embedding model
are optional and imported lazily. When they are unavailable the service falls back to a
deterministic lexical retriever over ``guideline_chunks`` (keyword + token overlap, normalised to
a 0..1 score) so retrieval and citation work offline and in tests. Either way every result keeps
its citation metadata (source, section_id, page_range, corpus_version).
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.guideline import GuidelineChunk

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {
    "the",
    "a",
    "an",
    "of",
    "for",
    "and",
    "or",
    "to",
    "in",
    "with",
    "management",
    "patient",
    "consider",
    "considering",
    "guidelines",
    "support",
}


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP and len(t) > 2}


def lexical_score(query: str, chunks: list[GuidelineChunk], k: int) -> list[dict]:
    """Deterministic lexical retriever: keyword hits (×1.5) + body token overlap, normalised.

    Shared by the async ``GuidelineService.retrieve`` and the sync retriever closure the
    ReasoningContext needs (it has no event loop / DB access).
    """
    q_tokens = _tokens(query)
    if not q_tokens or not chunks:
        return []
    scored: list[tuple[float, GuidelineChunk]] = []
    for chunk in chunks:
        kw = {str(x).lower() for x in (chunk.keywords or [])}
        body = _tokens(f"{chunk.heading or ''} {chunk.content}")
        kw_hits = sum(1 for t in q_tokens if t in kw or any(t in k2 for k2 in kw))
        overlap = len(q_tokens & body)
        # Saturating relevance: independent of query length so a long multi-diagnosis query
        # is not penalised. raw>=4.5 (≈3 keyword hits, or 2 hits + body overlap) clears 0.75.
        raw = 1.5 * kw_hits + overlap
        score = round(raw / (raw + 1.5), 4) if raw else 0.0
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [GuidelineService._to_dict(c, s) for s, c in scored[:k]]


class GuidelineService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _chunks(self, corpus_version: str | None) -> list[GuidelineChunk]:
        version = corpus_version or settings.guideline_corpus_version
        result = await self.db.execute(
            select(GuidelineChunk).where(GuidelineChunk.corpus_version == version)
        )
        return list(result.scalars().all())

    async def retrieve(
        self, query: str, k: int = 6, *, corpus_version: str | None = None
    ) -> list[dict]:
        """Return up to ``k`` chunk dicts with a 0..1 ``score`` (lexical fallback retriever)."""
        chunks = await self._chunks(corpus_version)
        return lexical_score(query, chunks, k)

    @staticmethod
    def _to_dict(chunk: GuidelineChunk, score: float) -> dict:
        return {
            "section_id": chunk.section_id,
            "source": chunk.source,
            "document_title": chunk.document_title,
            "heading": chunk.heading,
            "content": chunk.content,
            "page_range": chunk.page_range,
            "corpus_version": chunk.corpus_version,
            "score": score,
        }

    async def search(self, query: str, k: int = 10) -> list[dict]:
        """Public guideline search (no threshold filter) for the management/citation UI."""
        return await self.retrieve(query, k)

    async def count(self, corpus_version: str | None = None) -> int:
        chunks = await self._chunks(corpus_version)
        return len(chunks)
