"""What reading the guideline corpus costs, and what it is allowed to return.

The corpus is the one dataset in this application that is neither per-patient nor small. It is
the ICMR Standard Treatment Workflows plus WHO and NICE — thousands of chunks of full guideline
text — and the lexical retriever scores a query against all of it. Two things followed from
reading it naively, and both grew with the corpus rather than with the request:

* every retrieval transferred and instantiated the whole corpus, and
  ``ReasoningService._build_context`` does one per reasoning request — including the intake
  rounds that never retrieve anything;
* every query re-tokenised every chunk's heading and body inside the scoring loop, and a
  reasoning run queries once per hypothesis.

Caching it is safe because of what it is: curated reference data, published under a version,
identical for every patient and every account. Nothing per-request, nothing patient-specific.
But "cached" must not become "stale" — a management option citing a guideline the corpus no
longer carries is a clinical defect, not a cache-consistency curiosity — so the freshness check
is what most of this file is about.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import event

from app.models.guideline import GuidelineChunk
from app.services.guideline_service import GuidelineService, lexical_score

pytestmark = pytest.mark.asyncio

VERSION = "v-corpus-test"


def _chunk(section_id: str, *, content: str, keywords: list[str], version: str = VERSION):
    return GuidelineChunk(
        corpus_version=version,
        source="icmr",
        document_title="Standard Treatment Workflow",
        section_id=section_id,
        heading="Management",
        content=content,
        page_range="1-2",
        keywords=keywords,
    )


async def _seed(db, *chunks) -> None:
    for chunk in chunks:
        db.add(chunk)
    await db.flush()


async def _retrieve(service: GuidelineService, query: str) -> list[dict]:
    """Always version-pinned. The default version holds the seeded corpus, which would put
    unrelated chunks in every result here and make the counts below meaningless."""
    return await service.retrieve(query, 5, corpus_version=VERSION)


def _count_rows_read(engine):
    """Count statements that read ``guideline_chunks`` rows, ignoring the freshness aggregate.

    The distinction is the whole point: the cheap check is meant to run every time, and the
    expensive read is meant not to.
    """
    counter = {"full_reads": 0, "statements": []}

    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        normalised = " ".join(statement.split()).lower()
        counter["statements"].append(normalised)
        if "from guideline_chunks" in normalised and "count(" not in normalised:
            counter["full_reads"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", _on_execute)
    counter["_detach"] = lambda: event.remove(
        engine.sync_engine, "before_cursor_execute", _on_execute
    )
    return counter


# --- Cost -------------------------------------------------------------------------------


async def test_a_second_retrieval_does_not_re_read_the_corpus(db, engine):
    await _seed(
        db, _chunk("s1", content="Metformin first line in type 2 diabetes", keywords=["diabetes"])
    )
    service = GuidelineService(db)
    await _retrieve(service, "diabetes")

    counter = _count_rows_read(engine)
    try:
        await _retrieve(service, "diabetes")
        await _retrieve(service, "hypertension")
    finally:
        counter["_detach"]()

    assert counter["full_reads"] == 0, counter["statements"]


async def test_the_freshness_check_still_runs_on_every_retrieval(db, engine):
    """The cache is validated, not trusted. A cache that stopped asking would serve a corpus
    an ingestion has already replaced, and the caller would cite a superseded guideline."""
    await _seed(db, _chunk("s1", content="Metformin first line", keywords=["diabetes"]))
    service = GuidelineService(db)
    await _retrieve(service, "diabetes")

    counter = _count_rows_read(engine)
    try:
        await _retrieve(service, "diabetes")
    finally:
        counter["_detach"]()

    assert any("count(" in s for s in counter["statements"]), counter["statements"]


async def test_counting_the_corpus_does_not_load_it(db, engine):
    """``count()`` backs a status endpoint. It used to load every chunk, with its full
    guideline text, to call ``len()`` on the list."""
    await _seed(
        db,
        _chunk("s1", content="A" * 500, keywords=["diabetes"]),
        _chunk("s2", content="B" * 500, keywords=["hypertension"]),
    )

    counter = _count_rows_read(engine)
    try:
        total = await GuidelineService(db).count(corpus_version=VERSION)
    finally:
        counter["_detach"]()

    assert total == 2
    assert counter["full_reads"] == 0, counter["statements"]


async def test_chunk_tokens_are_computed_once_per_corpus_not_once_per_query(db):
    """The scoring loop reads ``body_tokens`` off the chunk rather than tokenising it. Two
    queries against one loaded corpus must therefore share the same frozenset object."""
    await _seed(
        db, _chunk("s1", content="Metformin first line in type 2 diabetes", keywords=["diabetes"])
    )
    service = GuidelineService(db)

    first = await service._load_corpus(VERSION)
    second = await service._load_corpus(VERSION)

    assert first[0].body_tokens is second[0].body_tokens


# --- Freshness ---------------------------------------------------------------------------


async def test_a_newly_ingested_chunk_is_retrievable_immediately(db):
    """Ingestion appends to a live corpus version. A stale cache here means a guideline that
    was published is invisible to every agent in this worker until it restarts."""
    await _seed(db, _chunk("s1", content="Metformin first line in diabetes", keywords=["diabetes"]))
    service = GuidelineService(db)
    assert [r["section_id"] for r in await _retrieve(service, "diabetes")] == ["s1"]

    await _seed(db, _chunk("s2", content="Amlodipine in hypertension", keywords=["hypertension"]))

    assert [r["section_id"] for r in await _retrieve(service, "hypertension")] == ["s2"]


async def test_an_edited_chunk_is_re_read_even_though_the_row_count_is_unchanged(db):
    """A correction to a guideline's text moves no counts, which is why the stamp carries a
    timestamp as well. Serving the superseded wording would be invisible downstream — the
    citation on it is unchanged, so a clinician reading the management option sees a current
    guideline reference attached to text the guideline no longer contains.

    ``updated_at`` is advanced explicitly rather than left to ``onupdate``. On PostgreSQL that
    is transaction-start time at microsecond resolution, so a re-ingestion always stamps later;
    on SQLite it is ``CURRENT_TIMESTAMP`` at one-second granularity, and an edit in the same
    second as the read is indistinguishable from no edit at all. That limitation is the test
    environment's, not the design's, and asserting around it here would assert nothing.
    """
    chunk = _chunk("s1", content="Metformin first line in diabetes", keywords=["diabetes"])
    await _seed(db, chunk)
    service = GuidelineService(db)
    await _retrieve(service, "diabetes")

    chunk.content = "Metformin first line in diabetes; hold during acute kidney injury"
    chunk.updated_at = chunk.updated_at + timedelta(seconds=30)
    await db.flush()

    (result,) = await _retrieve(service, "diabetes")
    assert "acute kidney injury" in result["content"]


async def test_a_deleted_chunk_stops_being_retrievable(db):
    chunk = _chunk("s1", content="Metformin first line in diabetes", keywords=["diabetes"])
    await _seed(db, chunk)
    service = GuidelineService(db)
    await _retrieve(service, "diabetes")

    await db.delete(chunk)
    await db.flush()

    assert await _retrieve(service, "diabetes") == []


async def test_two_corpus_versions_do_not_share_a_cache_entry(db):
    """The version is the cache key because it is the corpus's identity. Crossing them would
    cite the wrong edition of a guideline."""
    await _seed(
        db,
        _chunk(
            "s1", content="Metformin first line in diabetes", keywords=["diabetes"], version="v-a"
        ),
        _chunk(
            "s2", content="Metformin first line in diabetes", keywords=["diabetes"], version="v-b"
        ),
    )
    service = GuidelineService(db)

    assert [
        r["section_id"] for r in await service.retrieve("diabetes", 5, corpus_version="v-a")
    ] == ["s1"]
    assert [
        r["section_id"] for r in await service.retrieve("diabetes", 5, corpus_version="v-b")
    ] == ["s2"]


# --- Scoring is unchanged ------------------------------------------------------------------


async def test_ranking_still_puts_the_better_match_first(db):
    """The projection carries the tokens; it must not change what they score to."""
    await _seed(
        db,
        _chunk("weak", content="Diabetes is common", keywords=[]),
        _chunk(
            "strong",
            content="Metformin first line in type 2 diabetes mellitus",
            keywords=["diabetes", "metformin"],
        ),
    )

    ranked = await _retrieve(GuidelineService(db), "metformin diabetes")

    assert [r["section_id"] for r in ranked] == ["strong", "weak"]


async def test_citation_metadata_survives_the_projection(db):
    """Every chunk must keep its source, section and page range — a management option without
    them is an unciteable claim, which the Guideline-RAG agent is not allowed to make."""
    await _seed(db, _chunk("s1", content="Metformin first line in diabetes", keywords=["diabetes"]))

    (result,) = await _retrieve(GuidelineService(db), "diabetes")

    assert (
        result["source"],
        result["section_id"],
        result["page_range"],
        result["corpus_version"],
    ) == (
        "icmr",
        "s1",
        "1-2",
        VERSION,
    )


async def test_scoring_an_empty_corpus_is_not_an_error():
    assert lexical_score("diabetes", [], 5) == []
    assert lexical_score("", [], 5) == []
