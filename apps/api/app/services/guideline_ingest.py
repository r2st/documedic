"""Guideline corpus ingestion (Phase 3, architecture §7.2).

Reads curated guideline documents (``data/guidelines/*.json``), chunks them section-aware
(~512 tokens), preserves citation metadata, and upserts into ``guideline_chunks`` (idempotent on
corpus_version + source + section_id). Dense embeddings and the Qdrant upload are optional and
independent of each other: if ``sentence-transformers`` is installed the chunks are embedded into
``guideline_chunks.embedding`` (which is what ``guideline_service`` re-ranks with), and if
``qdrant-client`` is installed *and* the caller asks, they are also indexed into Qdrant.
Otherwise ingestion still populates the table so the deterministic lexical retriever works.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_sessionmaker
from app.models.guideline import _SOURCES, GuidelineChunk
from app.services import dense_retrieval

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


def _text(value: object) -> str:
    """A trimmed string, or ``""`` for anything that is not one.

    The corpus is hand-curated JSON, so every field is as likely to be the wrong *type* as to
    be missing — ``"content"`` written as a list of paragraphs is the obvious one — and
    ``(value or "").strip()`` raises on all of those rather than rejecting them.
    """
    return value.strip() if isinstance(value, str) else ""


def load_corpus(path: Path | None = None) -> list[dict]:
    """Flatten the curated corpus into chunk records (without corpus_version).

    Documents are validated before they reach the database: ``guideline_chunks.source`` has a
    CHECK constraint, and a typo'd source in a hand-curated JSON file would otherwise abort
    the whole ingest with an opaque IntegrityError halfway through. Malformed documents and
    sections are skipped with a warning so one bad entry cannot block the rest of the corpus.

    That last guarantee is enforced by *type*, not only by presence. This walks a hand-edited
    JSON file, and only two shapes used to be checked: a file whose top level was an object
    rather than an array iterated its keys and made ``doc.get`` an AttributeError, and a section
    whose ``content`` was written as a list of paragraphs made ``.strip()`` one. Neither is
    caught by the ``json.JSONDecodeError`` handler above -- the file parses fine, it is simply
    shaped differently -- and ``ingest`` runs from ``app.db.seed``, so a single mis-shaped
    corpus file failed startup seeding rather than one document.
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
        if not isinstance(docs, list):
            logger.warning(
                "Skipping guideline file %s: expected a list of documents, found %s",
                file.name,
                type(docs).__name__,
            )
            continue
        for doc in docs:
            if not isinstance(doc, dict):
                logger.warning(
                    "Skipping entry in %s: expected a document object, found %s",
                    file.name,
                    type(doc).__name__,
                )
                continue
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
            title = _text(doc.get("document_title"))
            if not title:
                logger.warning("Skipping guideline document in %s: no document_title", file.name)
                continue
            sections = doc.get("sections")
            for section in sections if isinstance(sections, list) else []:
                if not isinstance(section, dict):
                    logger.warning(
                        "Skipping section in %s (%r): expected an object, found %s",
                        file.name,
                        title,
                        type(section).__name__,
                    )
                    continue
                # ``section_id`` is the citation. A non-string one reached a String column and,
                # worse, is what every management option cites back — so it has to be a string
                # here rather than whatever ``f"{...}"`` would make of it downstream.
                base_id = _text(section.get("section_id"))
                content = _text(section.get("content"))
                if not base_id or not content:
                    logger.warning(
                        "Skipping section %r in %s: section_id and content are both required",
                        section.get("section_id"),
                        file.name,
                    )
                    continue
                keywords = section.get("keywords")
                # A bare string here is one keyword, not a list of letters -- which is what
                # iterating it downstream in ``RetrievableChunk.of`` would have made of it,
                # silently degrading every lexical match against this section.
                if isinstance(keywords, str):
                    keywords = [keywords]
                elif isinstance(keywords, (list | tuple)):
                    keywords = [k.strip() for k in keywords if isinstance(k, str) and k.strip()]
                else:
                    keywords = []
                pieces = chunk_text(content)
                for i, piece in enumerate(pieces):
                    section_id = base_id if len(pieces) == 1 else f"{base_id}-{i + 1}"
                    chunks.append(
                        {
                            "source": source,
                            "document_title": title,
                            "page_range": _text(doc.get("page_range")) or None,
                            "section_id": section_id,
                            "heading": _text(section.get("heading")) or None,
                            "content": piece,
                            "keywords": keywords,
                            "token_estimate": _token_estimate(piece),
                        }
                    )
    return chunks


def _key(record: dict) -> tuple[str, str]:
    return (record["source"], record["section_id"])


def _deduplicated(records: list[dict]) -> list[dict]:
    """One record per ``(source, section_id)``, keeping the first.

    A single guideline file can repeat a ``section_id`` -- the corpus is hand-curated -- and that
    key is the citation: it is what the insert loop tests against ``existing``, what
    ``_point_id`` hashes, and what a management option cites back. Two records sharing it are
    one chunk as far as every reader is concerned.

    This used to happen implicitly: the insert loop added each key to ``existing`` as it went, so
    the second copy hit the ``continue``. That stopped working when the loop began iterating a
    pre-computed ``new_records`` -- the set is built in one pass now, before any insert, so
    nothing updates it in between and both copies were inserted. Making it a step of its own is
    what keeps the two readers of that key agreeing, rather than a side effect of one of them.
    """
    unique: dict[tuple[str, str], dict] = {}
    for record in records:
        unique.setdefault(_key(record), record)
    return list(unique.values())


async def ingest(
    db: AsyncSession,
    *,
    corpus_version: str | None = None,
    path: Path | None = None,
    push_qdrant: bool = False,
) -> int:
    """Upsert the corpus into ``guideline_chunks``. Returns the number of new chunks.

    Chunks are embedded whenever a model is available, independently of ``push_qdrant``.

    Those two used to be the same switch, and only ``python -m app.services.guideline_ingest``
    ever passed it. Every other path -- ``db.seed.seed_all``, which is what runs at startup and
    in every deployment -- called ``ingest(db)``, so ``guideline_chunks.embedding`` was NULL for
    every row a real deployment ever had. That was invisible while nothing read the column; it
    is not now that the dense re-ranker does (``guideline_service.dense_scores``), because an
    un-embedded corpus makes it permanently inert. The vector stored in the row and the vector
    pushed to Qdrant serve different readers, so they are no longer gated on one flag.
    """
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
    records = _deduplicated(load_corpus(path))
    new_records = [r for r in records if _key(r) not in existing]

    # Qdrant is upserted with the whole corpus; the database column only needs the rows actually
    # being inserted. The distinction matters because ``ingest`` runs on every startup and is
    # idempotent -- embedding all of it each time would load a transformer and encode the entire
    # corpus to insert nothing, on the boot path of every deployment.
    to_embed = records if push_qdrant else new_records
    vectors: dict[tuple[str, str], tuple[float, ...]] = {}
    if to_embed:
        # Synchronous and slow: a transformer forward pass over the batch. ``ingest`` runs from
        # the seeding path and from the admin endpoint, on the event loop that is also serving
        # every other request, so it goes to a worker thread.
        encoded = await asyncio.to_thread(
            dense_retrieval.embed_documents,
            [dense_retrieval.embedding_text(r["heading"], r["content"]) for r in to_embed],
        )
        if encoded:
            vectors = {_key(r): v for r, v in zip(to_embed, encoded, strict=True)}

    added = 0
    for rec in new_records:
        vector = vectors.get(_key(rec))
        db.add(
            GuidelineChunk(
                corpus_version=version,
                embedding=list(vector) if vector else None,
                **rec,
            )
        )
        added += 1
    await db.flush()

    if push_qdrant and vectors:
        # Blocking HTTP, same reasoning as the encode above.
        await asyncio.to_thread(_push_qdrant, version, records, vectors)
    return added


def _point_id(version: str, record: dict) -> str:
    """A stable point id for a chunk: same corpus coordinates -> same point, every run.

    The index used to be dropped and rebuilt on every push, so a point's id could be its
    position in ``records`` and nothing depended on it surviving. Upserting into a live
    collection does depend on it: a positional id re-points an existing vector at whatever
    chunk happens to sit at that offset in the next run, so a corpus that gained or lost a
    section would silently re-label every point after it. Deriving the id from the citation
    coordinates instead makes a re-push of the same chunk overwrite *that* chunk.
    """
    key = f"{version}:{record.get('source')}:{record.get('section_id')}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _collection_exists(client: object, collection: str) -> bool:
    """Whether the collection is already present, across qdrant-client versions."""
    exists = getattr(client, "collection_exists", None)
    if callable(exists):
        return bool(exists(collection))
    try:  # older clients: no collection_exists, raises for a missing collection
        client.get_collection(collection)  # type: ignore[attr-defined]
    except Exception:
        return False
    return True


def _push_qdrant(
    version: str, records: list[dict], vectors: dict[tuple[str, str], tuple[float, ...]]
) -> None:
    """Index chunks into Qdrant if the client is available (best-effort).

    Creates the versioned collection only when it is missing, then upserts. This used to call
    ``recreate_collection``, which *drops* the collection before repopulating it: any failure
    between the drop and the end of the upsert -- a network blip, a dimension mismatch, the
    process being killed mid-ingest -- left the deployment with no vector index at all, and
    retrieval silently degraded to the lexical fallback until someone re-ran the ingest. An
    upsert into an existing collection is idempotent (see ``_point_id``) and never leaves the
    index emptier than it found it.
    """
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, PointStruct, VectorParams
    except Exception:
        return
    # Only the chunks a vector was produced for. With the embedding batch now scoped to what it
    # is for (see ``ingest``), ``records`` can legitimately contain rows this run did not embed.
    indexable = [r for r in records if _key(r) in vectors]
    if not indexable:
        return
    client = QdrantClient(url=settings.qdrant_url)
    collection = f"{settings.qdrant_collection}_{version}".replace(".", "_")
    dim = len(vectors[_key(indexable[0])])
    if not _collection_exists(client, collection):
        try:
            client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
        except Exception as exc:
            # A concurrent ingest may have created it between the check and here. That is fine
            # -- the upsert below is what actually has to succeed, and it will raise if the
            # collection genuinely is not there.
            logger.warning("Could not create Qdrant collection %s: %s", collection, exc)
    client.upsert(
        collection_name=collection,
        points=[
            PointStruct(
                id=_point_id(version, rec),
                vector=list(vectors[_key(rec)]),
                payload=rec,
            )
            for rec in indexable
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
