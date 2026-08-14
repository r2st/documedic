"""Guideline corpus ingestion: chunking, citation metadata, and idempotent upsert.

Citation metadata is the load-bearing part — the Guideline-RAG Agent cannot cite what
ingestion did not preserve, and an uncited guideline claim is a hallucination by definition.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import types

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.guideline import GuidelineChunk
from app.services import dense_retrieval, guideline_ingest
from app.services.guideline_ingest import chunk_text, ingest, load_corpus

_DOC = {
    "source": "icmr",
    "document_title": "Standard Treatment Workflow: Hypertension",
    "page_range": "12-14",
    "sections": [
        {
            "section_id": "htn-first-line",
            "heading": "First-line pharmacotherapy",
            "content": "Amlodipine, telmisartan or enalapril are appropriate first-line agents.",
            "keywords": ["hypertension", "first-line"],
        },
        {
            "section_id": "htn-targets",
            "heading": "Blood-pressure targets",
            "content": "A target below 140/90 mmHg is appropriate for most adults.",
            "keywords": ["target"],
        },
    ],
}


@pytest.fixture
def corpus_file(tmp_path):
    path = tmp_path / "hypertension.json"
    path.write_text(json.dumps([_DOC]))
    return path


async def _rows(db, corpus_version: str = "test-1.0"):
    """Rows this test ingested, in insert order.

    Scoped to the version deliberately: the session fixture arrives with the real shipped corpus
    already seeded, so an unscoped ``select(GuidelineChunk)`` also returns those rows -- and they
    have no embedding, which is enough to make an "embeddings are populated" assertion fail and
    an "embeddings are empty" one pass for a reason that has nothing to do with the test.
    """
    result = await db.execute(
        select(GuidelineChunk)
        .where(GuidelineChunk.corpus_version == corpus_version)
        .order_by(GuidelineChunk.id)
    )
    return list(result.scalars().all())


# --- chunk_text ------------------------------------------------------------------------


def test_short_sections_stay_a_single_chunk():
    assert chunk_text("A short clinical statement.") == ["A short clinical statement."]


def test_long_sections_split_on_sentence_boundaries():
    text = " ".join(f"Sentence number {i} about clinical management." for i in range(200))
    chunks = chunk_text(text, max_tokens=50)
    assert len(chunks) > 1
    # Splitting mid-sentence would strand a clause in a citation; every chunk ends cleanly.
    assert all(chunk.endswith(".") for chunk in chunks)


def test_chunking_preserves_all_content():
    text = " ".join(f"Sentence {i} here." for i in range(80))
    rejoined = " ".join(chunk_text(text, max_tokens=30))
    assert rejoined.split() == text.split()


def test_a_single_oversized_sentence_is_not_dropped():
    """No sentence boundary to split on — better one long chunk than silent data loss."""
    text = "word " * 800
    chunks = chunk_text(text, max_tokens=100)
    assert chunks and "".join(chunks).count("word") == 800


def test_empty_text_yields_one_empty_chunk():
    assert chunk_text("") == [""]


# --- load_corpus -----------------------------------------------------------------------


def test_load_corpus_flattens_sections_into_chunk_records(corpus_file):
    records = load_corpus(corpus_file)
    assert len(records) == 2
    assert {r["section_id"] for r in records} == {"htn-first-line", "htn-targets"}


def test_every_chunk_carries_its_citation_metadata(corpus_file):
    for record in load_corpus(corpus_file):
        assert record["source"] == "icmr"
        assert record["document_title"].startswith("Standard Treatment Workflow")
        assert record["page_range"] == "12-14"
        assert record["heading"]
        assert record["token_estimate"] > 0


def test_split_sections_get_suffixed_section_ids(tmp_path):
    """A section that splits must still cite distinctly, or two chunks collide on upsert."""
    doc = {
        "source": "who",
        "document_title": "Essential Medicines",
        "sections": [
            {
                "section_id": "long-section",
                "heading": "Long",
                "content": " ".join(f"Clinical sentence number {i}." for i in range(400)),
            }
        ],
    }
    path = tmp_path / "who.json"
    path.write_text(json.dumps([doc]))

    ids = [r["section_id"] for r in load_corpus(path)]
    assert len(ids) > 1
    assert ids[0] == "long-section-1"
    assert len(set(ids)) == len(ids)


def test_a_missing_file_yields_no_records(tmp_path):
    assert load_corpus(tmp_path / "absent.json") == []


def test_keywords_default_to_empty_when_absent(tmp_path):
    doc = {
        "source": "nice",
        "document_title": "Guideline",
        "sections": [{"section_id": "s1", "content": "Some guidance."}],
    }
    path = tmp_path / "nice.json"
    path.write_text(json.dumps([doc]))
    assert load_corpus(path)[0]["keywords"] == []


def test_a_document_with_an_unknown_source_is_skipped(tmp_path):
    """guideline_chunks.source has a CHECK constraint — a typo'd source used to reach the
    DB and abort the whole ingest with an IntegrityError partway through."""
    doc = {
        "source": "ICMR STW",  # human-readable label, not the enum value
        "document_title": "Doc",
        "sections": [{"section_id": "s1", "content": "Guidance."}],
    }
    path = tmp_path / "bad-source.json"
    path.write_text(json.dumps([doc]))
    assert load_corpus(path) == []


def test_one_bad_document_does_not_block_the_rest_of_the_file(tmp_path):
    docs = [
        {
            "source": "not-a-source",
            "document_title": "Bad",
            "sections": [{"section_id": "b1", "content": "Guidance."}],
        },
        {
            "source": "icmr",
            "document_title": "Good",
            "sections": [{"section_id": "g1", "content": "Guidance."}],
        },
    ]
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps(docs))
    records = load_corpus(path)
    assert [r["section_id"] for r in records] == ["g1"]


def test_a_document_without_a_title_is_skipped(tmp_path):
    doc = {"source": "icmr", "sections": [{"section_id": "s1", "content": "Guidance."}]}
    path = tmp_path / "untitled.json"
    path.write_text(json.dumps([doc]))
    assert load_corpus(path) == []


def test_sections_without_an_id_or_content_are_skipped(tmp_path):
    doc = {
        "source": "icmr",
        "document_title": "Doc",
        "sections": [
            {"content": "No id here."},
            {"section_id": "blank", "content": "   "},
            {"section_id": "ok", "content": "Real guidance."},
        ],
    }
    path = tmp_path / "ragged.json"
    path.write_text(json.dumps([doc]))
    assert [r["section_id"] for r in load_corpus(path)] == ["ok"]


def test_a_malformed_json_file_is_skipped_not_raised(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ this is not json")
    assert load_corpus(path) == []


# --- ingest ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_writes_chunks(db, corpus_file):
    added = await ingest(db, corpus_version="test-1.0", path=corpus_file)
    assert added == 2

    rows = (
        (
            await db.execute(
                select(GuidelineChunk).where(GuidelineChunk.corpus_version == "test-1.0")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert all(row.source == "icmr" for row in rows)


@pytest.mark.asyncio
async def test_ingest_is_idempotent(db, corpus_file):
    assert await ingest(db, corpus_version="test-1.0", path=corpus_file) == 2
    assert await ingest(db, corpus_version="test-1.0", path=corpus_file) == 0


@pytest.mark.asyncio
async def test_corpus_versions_are_isolated(db, corpus_file):
    """A new corpus version re-ingests everything so old citations stay resolvable."""
    await ingest(db, corpus_version="test-1.0", path=corpus_file)
    assert await ingest(db, corpus_version="test-2.0", path=corpus_file) == 2


# --- what gets embedded, and when --------------------------------------------------------
#
# Embedding used to be gated on ``push_qdrant``, which only the ``__main__`` entry point ever
# passed. ``db.seed.seed_all`` -- the path that actually runs on every deployment -- calls
# ``ingest(db)``, so ``guideline_chunks.embedding`` was NULL for every row in production and the
# dense re-ranker (``guideline_service.dense_scores``) was permanently inert. These tests inject
# the embedder rather than relying on a real model: with no model reachable the real one returns
# None, and a test that asserts "embeddings are None" then passes whatever the code does.


@pytest.mark.asyncio
async def test_ingest_embeds_without_being_asked_to_push_to_qdrant(db, corpus_file, monkeypatch):
    """The stored vector and the Qdrant point serve different readers and are not one switch."""
    monkeypatch.setattr(dense_retrieval, "embed_documents", lambda texts: [(1.0,)] * len(texts))

    await ingest(db, corpus_version="test-1.0", path=corpus_file, push_qdrant=False)

    rows = await _rows(db)
    assert len(rows) == 2
    assert all(row.embedding == [1.0] for row in rows)


@pytest.mark.asyncio
async def test_ingest_embeds_the_heading_with_the_content(db, corpus_file, monkeypatch):
    """A section's heading names what it is *for*, which is what a management query asks about."""
    seen: list[list[str]] = []

    def _embed(texts):
        seen.append(list(texts))
        return [(1.0,)] * len(texts)

    monkeypatch.setattr(dense_retrieval, "embed_documents", _embed)

    await ingest(db, corpus_version="test-1.0", path=corpus_file)

    assert seen, "the embedder was never called"
    assert "First-line pharmacotherapy Amlodipine" in seen[0][0]


@pytest.mark.asyncio
async def test_a_reingest_that_adds_nothing_does_not_load_the_model(db, corpus_file, monkeypatch):
    """``ingest`` runs on every startup. Encoding the whole corpus to insert nothing is the cost
    this avoids -- it would put a transformer load on the boot path of every deployment."""
    await ingest(db, corpus_version="test-1.0", path=corpus_file)

    calls: list[list[str]] = []
    monkeypatch.setattr(dense_retrieval, "embed_documents", lambda texts: calls.append(texts))

    assert await ingest(db, corpus_version="test-1.0", path=corpus_file) == 0
    assert calls == []


@pytest.mark.asyncio
async def test_ingest_stores_no_embedding_when_no_model_is_available(db, corpus_file, monkeypatch):
    """The deterministic lexical retriever is the offline-safe floor (Critical Safety Rule #8):
    a deployment with no torch installed ingests normally and simply has nothing to re-rank."""
    monkeypatch.setattr(dense_retrieval, "embed_documents", lambda texts: None)

    assert await ingest(db, corpus_version="test-1.0", path=corpus_file) == 2

    rows = await _rows(db)
    assert len(rows) == 2
    assert all(row.embedding is None for row in rows)


@pytest.mark.asyncio
async def test_ingest_of_an_empty_corpus_adds_nothing(db, tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("[]")
    assert await ingest(db, corpus_version="test-1.0", path=path) == 0


@pytest.mark.asyncio
async def test_duplicate_sections_within_one_file_are_collapsed(db, tmp_path):
    doc = {
        "source": "icmr",
        "document_title": "Doc",
        "sections": [
            {"section_id": "dup", "content": "First copy."},
            {"section_id": "dup", "content": "Second copy."},
        ],
    }
    path = tmp_path / "dup.json"
    path.write_text(json.dumps([doc]))
    assert await ingest(db, corpus_version="test-1.0", path=path) == 1

    # The count and the table have to agree. The count comes out of the insert loop, so a loop
    # that inserted both copies and happened to report one would satisfy the assertion above
    # while leaving two rows behind the same citation -- which is how the collapse broke once
    # already, when it stopped being a side effect of the loop updating ``existing``.
    rows = await _rows(db)
    assert [r.section_id for r in rows] == ["dup"]
    assert rows[0].content == "First copy."


# --- ingest does not block the event loop ------------------------------------------------
#
# ``dense_retrieval.embed_documents`` runs SentenceTransformer over the batch and ``_push_qdrant``
# is blocking HTTP. Both used to be called directly from ``async def ingest``, which runs on the
# loop that is also serving every other request -- so a seed or an admin re-ingest froze the
# API for the duration. They are off-loaded with ``asyncio.to_thread`` now.


@pytest.mark.asyncio
async def test_embedding_runs_on_a_worker_thread(db, corpus_file, monkeypatch):
    seen: list[threading.Thread] = []

    def _embed(texts):
        seen.append(threading.current_thread())
        return [(0.1,)] * len(texts)

    monkeypatch.setattr(dense_retrieval, "embed_documents", _embed)
    monkeypatch.setattr(guideline_ingest, "_push_qdrant", lambda *a: None)

    await ingest(db, corpus_version="test-1.0", path=corpus_file, push_qdrant=True)

    assert seen, "the embedder was never called"
    assert seen[0] is not threading.main_thread()


@pytest.mark.asyncio
async def test_the_qdrant_push_runs_on_a_worker_thread(db, corpus_file, monkeypatch):
    seen: list[threading.Thread] = []

    monkeypatch.setattr(dense_retrieval, "embed_documents", lambda texts: [(0.1,)] * len(texts))
    monkeypatch.setattr(
        guideline_ingest,
        "_push_qdrant",
        lambda version, records, embeddings: seen.append(threading.current_thread()),
    )

    await ingest(db, corpus_version="test-1.0", path=corpus_file, push_qdrant=True)

    assert seen, "the vector push was never called"
    assert seen[0] is not threading.main_thread()


@pytest.mark.asyncio
async def test_a_slow_ingest_leaves_the_event_loop_responsive(db, corpus_file, monkeypatch):
    """The point of the off-load: other requests keep being served during an ingestion.

    Asserted behaviourally rather than by inspecting the call — a blocking ``time.sleep`` in
    the embedder must not stop a concurrently scheduled coroutine from making progress.
    """
    ticks = 0

    async def _heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    def _slow_embed(texts):
        time.sleep(0.3)
        return [(0.1,)] * len(texts)

    monkeypatch.setattr(dense_retrieval, "embed_documents", _slow_embed)
    monkeypatch.setattr(guideline_ingest, "_push_qdrant", lambda *a: None)

    beat = asyncio.create_task(_heartbeat())
    try:
        await ingest(db, corpus_version="test-1.0", path=corpus_file, push_qdrant=True)
    finally:
        beat.cancel()

    # ~30 ticks are possible; anything above a couple proves the loop was never parked.
    assert ticks >= 3, f"the event loop was blocked during ingestion (ticks={ticks})"


# --- the Qdrant push upserts, it does not drop the index ----------------------------------
#
# ``_push_qdrant`` used to call ``recreate_collection``, which deletes the collection before
# repopulating it. Anything that failed in between -- a network blip, a killed process, a
# dimension mismatch -- left the deployment with *no* vector index, silently degrading
# retrieval to the lexical fallback until someone noticed and re-ran the ingest.


class _FakeQdrantClient:
    """Records what a push did to the collection. Mirrors the qdrant-client surface used."""

    def __init__(self, url, *, exists: bool = False, create_error: Exception | None = None):
        self.url = url
        self.calls: list[str] = []
        self._exists = exists
        self._create_error = create_error
        self.created: dict | None = None
        self.points: list = []

    def collection_exists(self, collection_name):
        self.calls.append("collection_exists")
        return self._exists

    def create_collection(self, collection_name, vectors_config):
        self.calls.append("create_collection")
        if self._create_error is not None:
            raise self._create_error
        self.created = {"name": collection_name, "size": vectors_config.size}
        self._exists = True

    def upsert(self, collection_name, points):
        self.calls.append("upsert")
        self.points = list(points)


def _install_fake_qdrant(monkeypatch, client: object) -> None:
    fake_models = types.SimpleNamespace(
        Distance=types.SimpleNamespace(COSINE="Cosine"),
        PointStruct=lambda id, vector, payload: types.SimpleNamespace(
            id=id, vector=vector, payload=payload
        ),
        VectorParams=lambda size, distance: types.SimpleNamespace(size=size, distance=distance),
    )
    monkeypatch.setitem(
        sys.modules, "qdrant_client", types.SimpleNamespace(QdrantClient=lambda url: client)
    )
    monkeypatch.setitem(sys.modules, "qdrant_client.models", fake_models)


_RECORDS = [
    {"source": "icmr", "section_id": "htn-first-line", "content": "Amlodipine."},
    {"source": "icmr", "section_id": "htn-targets", "content": "Below 140/90."},
]
# Keyed by ``(source, section_id)`` rather than by position: ``ingest`` no longer embeds the whole
# corpus on every run, so the batch it hands to ``_push_qdrant`` can cover only some of
# ``records`` and a positional pairing would hand a chunk another chunk's vector.
_VECTORS = {
    ("icmr", "htn-first-line"): (0.1, 0.2, 0.3),
    ("icmr", "htn-targets"): (0.4, 0.5, 0.6),
}


def test_a_missing_collection_is_created_then_upserted(monkeypatch):
    monkeypatch.setattr(settings, "qdrant_collection", "guidelines")
    monkeypatch.setattr(settings, "qdrant_url", "http://qdrant:6333")
    client = _FakeQdrantClient("http://qdrant:6333", exists=False)
    _install_fake_qdrant(monkeypatch, client)

    guideline_ingest._push_qdrant("icmr-2024.1", _RECORDS, _VECTORS)

    assert client.created == {"name": "guidelines_icmr-2024_1", "size": 3}  # dots are illegal
    assert len(client.points) == 2
    assert "recreate_collection" not in client.calls


def test_an_existing_collection_is_upserted_into_never_recreated(monkeypatch):
    """The regression this whole change exists for: no drop of a live index."""
    client = _FakeQdrantClient("http://qdrant:6333", exists=True)
    _install_fake_qdrant(monkeypatch, client)

    guideline_ingest._push_qdrant("icmr-2024.1", _RECORDS, _VECTORS)

    assert client.calls == ["collection_exists", "upsert"]
    assert client.created is None, "an existing collection was re-created"
    assert len(client.points) == 2


def test_the_upsert_still_runs_when_creating_the_collection_raced_and_failed(monkeypatch):
    """A concurrent ingest can create the collection between the check and the create."""
    client = _FakeQdrantClient(
        "http://qdrant:6333", exists=False, create_error=RuntimeError("already exists")
    )
    _install_fake_qdrant(monkeypatch, client)

    guideline_ingest._push_qdrant("icmr-2024.1", _RECORDS, _VECTORS)

    assert client.calls == ["collection_exists", "create_collection", "upsert"]
    assert len(client.points) == 2


def test_an_older_client_without_collection_exists_is_probed_with_get_collection(monkeypatch):
    class _OldClient(_FakeQdrantClient):
        collection_exists = None  # type: ignore[assignment]  # absent on older qdrant-client

        def get_collection(self, collection_name):
            self.calls.append("get_collection")
            if not self._exists:
                raise RuntimeError("Not found")
            return {"name": collection_name}

    missing = _OldClient("http://qdrant:6333", exists=False)
    _install_fake_qdrant(monkeypatch, missing)
    guideline_ingest._push_qdrant("icmr-2024.1", _RECORDS, _VECTORS)
    assert missing.calls == ["get_collection", "create_collection", "upsert"]

    present = _OldClient("http://qdrant:6333", exists=True)
    _install_fake_qdrant(monkeypatch, present)
    guideline_ingest._push_qdrant("icmr-2024.1", _RECORDS, _VECTORS)
    assert present.calls == ["get_collection", "upsert"]


def test_point_ids_are_stable_across_pushes_of_the_same_chunk(monkeypatch):
    """Upserting into a live collection means ids have to identify the chunk, not its offset."""
    first = _FakeQdrantClient("u", exists=True)
    _install_fake_qdrant(monkeypatch, first)
    guideline_ingest._push_qdrant("icmr-2024.1", _RECORDS, _VECTORS)

    second = _FakeQdrantClient("u", exists=True)
    _install_fake_qdrant(monkeypatch, second)
    guideline_ingest._push_qdrant("icmr-2024.1", _RECORDS, _VECTORS)

    assert [p.id for p in first.points] == [p.id for p in second.points]


def test_a_chunk_keeps_its_point_id_when_the_corpus_grows_in_front_of_it(monkeypatch):
    """A positional id would re-point an existing vector at a different guideline section."""
    before = _FakeQdrantClient("u", exists=True)
    _install_fake_qdrant(monkeypatch, before)
    guideline_ingest._push_qdrant("icmr-2024.1", _RECORDS, _VECTORS)
    original = {p.payload["section_id"]: p.id for p in before.points}

    grown = [{"source": "icmr", "section_id": "htn-new", "content": "New."}, *_RECORDS]
    after = _FakeQdrantClient("u", exists=True)
    _install_fake_qdrant(monkeypatch, after)
    guideline_ingest._push_qdrant(
        "icmr-2024.1", grown, {("icmr", "htn-new"): (0.7, 0.8, 0.9), **_VECTORS}
    )

    shifted = {p.payload["section_id"]: p.id for p in after.points}
    assert shifted["htn-first-line"] == original["htn-first-line"]
    assert shifted["htn-targets"] == original["htn-targets"]
    assert shifted["htn-new"] not in original.values()


def test_chunks_with_no_vector_are_left_out_of_the_push(monkeypatch):
    """``records`` can legitimately run ahead of the batch that was embedded. Those chunks are
    skipped rather than paired with whatever vector sits at their index."""
    client = _FakeQdrantClient("u", exists=True)
    _install_fake_qdrant(monkeypatch, client)

    partial = [{"source": "icmr", "section_id": "htn-new", "content": "New."}, *_RECORDS]
    guideline_ingest._push_qdrant("icmr-2024.1", partial, _VECTORS)

    assert [p.payload["section_id"] for p in client.points] == ["htn-first-line", "htn-targets"]
    assert [p.vector for p in client.points] == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


def test_nothing_is_pushed_when_no_chunk_carries_a_vector(monkeypatch):
    """No embedder means no dense index to maintain -- and no empty collection created for one."""
    client = _FakeQdrantClient("u", exists=False)
    _install_fake_qdrant(monkeypatch, client)

    guideline_ingest._push_qdrant("icmr-2024.1", _RECORDS, {})

    assert client.calls == []


def test_point_ids_separate_corpus_versions_sources_and_sections():
    rec = {"source": "icmr", "section_id": "htn-first-line"}
    base = guideline_ingest._point_id("icmr-2024.1", rec)

    assert base == guideline_ingest._point_id("icmr-2024.1", dict(rec))
    assert base != guideline_ingest._point_id("icmr-2025.1", rec)
    assert base != guideline_ingest._point_id("icmr-2024.1", {**rec, "source": "who"})
    assert base != guideline_ingest._point_id("icmr-2024.1", {**rec, "section_id": "htn-targets"})
