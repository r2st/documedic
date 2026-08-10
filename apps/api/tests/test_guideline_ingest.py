"""Guideline corpus ingestion: chunking, citation metadata, and idempotent upsert.

Citation metadata is the load-bearing part — the Guideline-RAG Agent cannot cite what
ingestion did not preserve, and an uncited guideline claim is a hallucination by definition.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models.guideline import GuidelineChunk
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


@pytest.mark.asyncio
async def test_ingest_without_qdrant_leaves_embeddings_empty(db, corpus_file):
    """The lexical retriever has to work with no vector store present (offline-capable)."""
    await ingest(db, corpus_version="test-1.0", path=corpus_file, push_qdrant=False)
    rows = (await db.execute(select(GuidelineChunk))).scalars().all()
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
