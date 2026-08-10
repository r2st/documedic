"""Guideline corpus ingestion (Phase 3, architecture §7.2).

Reads curated guideline documents (``data/guidelines/*.json``), chunks them section-aware
(~512 tokens), preserves citation metadata, and upserts into ``guideline_chunks`` (idempotent on
corpus_version + source + section_id). Dense embeddings and the Qdrant upload are optional: if
``sentence-transformers`` / ``qdrant-client`` are installed the chunks are embedded and indexed;
otherwise ingestion still populates the table so the deterministic lexical retriever works.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_sessionmaker
from app.models.guideline import _SOURCES, GuidelineChunk

logger = logging.getLogger(__name__)

GUIDELINES_DIR = Path(__file__).resolve().parents[4] / "data" / "guidelines"
# Mirrors the guideline_chunks.source CHECK constraint — validated here so a bad corpus file
# is reported per-document instead of aborting the transaction at flush time.
ALLOWED_SOURCES = frozenset(_SOURCES)
_MAX_CHUNK_TOKENS = 512
_WORD = re.compile(r"\S+")


def _token_estimate(text: str) -> int:
    return len(_WORD.findall(text))


def chunk_text(text: str, max_tokens: int = _MAX_CHUNK_TOKENS) -> list[str]:
    """Split a section into ~max_tokens chunks on sentence boundaries (section-aware)."""
    if _token_estimate(text) <= max_tokens:
        return [text.strip()]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current: list[str] = []
    count = 0
    for sentence in sentences:
        n = _token_estimate(sentence)
        if count + n > max_tokens and current:
            chunks.append(" ".join(current).strip())
            current, count = [], 0
        current.append(sentence)
        count += n
    if current:
        chunks.append(" ".join(current).strip())
    return chunks


def load_corpus(path: Path | None = None) -> list[dict]:
    """Flatten the curated corpus into chunk records (without corpus_version).

    Documents are validated before they reach the database: ``guideline_chunks.source`` has a
    CHECK constraint, and a typo'd source in a hand-curated JSON file would otherwise abort
    the whole ingest with an opaque IntegrityError halfway through. Malformed documents and
    sections are skipped with a warning so one bad entry cannot block the rest of the corpus.
    """
    chunks: list[dict] = []
    files = [path] if path else sorted(GUIDELINES_DIR.glob("*.json"))
    for file in files:
        if file is None or not file.exists():
            continue
        try:
            docs = json.loads(file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping unreadable guideline file %s: %s", file.name, exc)
            continue
        for doc in docs:
            source = doc.get("source")
            if source not in ALLOWED_SOURCES:
                logger.warning(
                    "Skipping guideline document %r in %s: source %r is not one of %s",
                    doc.get("document_title"),
                    file.name,
                    source,
                    ", ".join(sorted(ALLOWED_SOURCES)),
                )
                continue
            if not doc.get("document_title"):
                logger.warning("Skipping guideline document in %s: no document_title", file.name)
                continue
            for section in doc.get("sections", []):
                if not section.get("section_id") or not (section.get("content") or "").strip():
                    logger.warning(
                        "Skipping section %r in %s: section_id and content are both required",
                        section.get("section_id"),
                        file.name,
                    )
                    continue
                pieces = chunk_text(section["content"])
                for i, piece in enumerate(pieces):
                    section_id = (
                        section["section_id"]
                        if len(pieces) == 1
                        else f"{section['section_id']}-{i + 1}"
                    )
                    chunks.append(
                        {
                            "source": doc["source"],
                            "document_title": doc["document_title"],
                            "page_range": doc.get("page_range"),
                            "section_id": section_id,
                            "heading": section.get("heading"),
                            "content": piece,
                            "keywords": section.get("keywords", []),
                            "token_estimate": _token_estimate(piece),
                        }
                    )
    return chunks


async def ingest(
    db: AsyncSession,
    *,
    corpus_version: str | None = None,
    path: Path | None = None,
    push_qdrant: bool = False,
) -> int:
    """Upsert the corpus into ``guideline_chunks``. Returns the number of new chunks."""
    version = corpus_version or settings.guideline_corpus_version
    existing = {
        tuple(row)
        for row in (
            await db.execute(
                select(GuidelineChunk.source, GuidelineChunk.section_id).where(
                    GuidelineChunk.corpus_version == version
                )
            )
        ).all()
    }
    records = load_corpus(path)
    embeddings = _maybe_embed([r["content"] for r in records]) if push_qdrant else None

    added = 0
    for idx, rec in enumerate(records):
        if (rec["source"], rec["section_id"]) in existing:
            continue
        db.add(
            GuidelineChunk(
                corpus_version=version,
                embedding=embeddings[idx] if embeddings else None,
                **rec,
            )
        )
        existing.add((rec["source"], rec["section_id"]))
        added += 1
    await db.flush()

    if push_qdrant and embeddings:
        _push_qdrant(version, records, embeddings)
    return added


def _maybe_embed(texts: list[str]) -> list[list[float]] | None:
    """Embed with sentence-transformers if available; otherwise return None (lexical fallback)."""
    try:
        from sentence_transformers import SentenceTransformer
    except Exception:
        return None
    model = SentenceTransformer("all-MiniLM-L6-v2")
    return [v.tolist() for v in model.encode(texts)]


def _push_qdrant(version: str, records: list[dict], embeddings: list[list[float]]) -> None:
    """Index chunks into Qdrant if the client is available (best-effort)."""
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, PointStruct, VectorParams
    except Exception:
        return
    client = QdrantClient(url=settings.qdrant_url)
    collection = f"{settings.qdrant_collection}_{version}".replace(".", "_")
    dim = len(embeddings[0])
    client.recreate_collection(
        collection_name=collection,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )
    client.upsert(
        collection_name=collection,
        points=[
            PointStruct(id=i, vector=embeddings[i], payload=records[i]) for i in range(len(records))
        ],
    )


async def _main() -> None:  # pragma: no cover — `python -m app.services.guideline_ingest`
    sm = get_sessionmaker()
    async with sm() as db:
        added = await ingest(db, push_qdrant=True)
        await db.commit()
    print(f"Ingested {added} guideline chunks (corpus {settings.guideline_corpus_version}).")


if __name__ == "__main__":  # pragma: no cover — module entrypoint
    asyncio.run(_main())
