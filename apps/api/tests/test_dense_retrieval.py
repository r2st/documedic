"""The dense half of guideline retrieval: the vector helpers, and the bound on what they may do.

Dense retrieval was built and never connected, on either side. ``requirements.txt`` pinned
``sentence-transformers`` and ``qdrant-client``; three module docstrings said production retrieval
*was* dense search with lexical as the offline fallback. Nothing in the application ever read an
embedding — and nothing ever wrote one either, since ``guideline_ingest`` embedded only when its
caller asked for a Qdrant push and no deployment path did. There was no dense code path to test,
which is why nothing here failed.

Two things are tested. First the vector helpers, which parse data written by an optional ingest
path and read back through whatever JSON driver the deployment uses -- so they are the boundary
where a half-migrated corpus turns into a crash or into a dropped re-ranking.

Then the invariant that makes dense retrieval safe to switch on at all: **dense similarity may
reorder results, and may never change which of them are citable.** That is not a tuning
preference. ``tests/test_retrieval_quality_benchmark.py`` measures why -- on the shipped corpus
there is no cosine cut that separates a genuine paraphrased match from an off-corpus query, so
any weight large enough to buy recall also grounds a management option in the wrong condition's
guideline.
"""

from __future__ import annotations

import math
import sys
import threading
import time
import types

import pytest

from app.models.guideline import GuidelineChunk
from app.services import dense_retrieval
from app.services.guideline_service import (
    RetrievableChunk,
    apply_dense_rerank,
    dense_scores,
    lexical_score,
)

THRESHOLD = 0.75


@pytest.fixture(autouse=True)
def _clean_model_state():
    """Each test starts with no model loaded and no remembered failure."""
    dense_retrieval.reset()
    yield
    dense_retrieval.reset()


# --- stored-vector parsing -----------------------------------------------------------------


def test_a_stored_embedding_is_normalised_to_a_unit_vector():
    """Chunk vectors and query vectors have to be on the same scale for a dot product to be a
    cosine. The ingest writes whatever the model produced, so normalising happens on read."""
    vector = dense_retrieval.as_vector([3.0, 4.0])

    assert vector is not None
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0)
    assert math.isclose(vector[0], 0.6) and math.isclose(vector[1], 0.8)


@pytest.mark.parametrize(
    ("value", "why"),
    [
        (None, "a corpus ingested before embeddings existed has NULL here"),
        ([], "an empty list has no direction to normalise"),
        ([0.0, 0.0], "a zero vector cannot be normalised"),
        (["0.1", "0.2"], "JSON that came back as strings is not a vector"),
        ([0.1, None], "a null component makes the whole vector unusable"),
        ([True, False], "bools are ints in Python and are never a real embedding"),
        ([float("nan"), 1.0], "NaN would poison every similarity it touches"),
        ([float("inf"), 1.0], "an infinite component has no finite norm"),
        ("[0.1, 0.2]", "a JSON string that was never decoded"),
        ({"v": [0.1]}, "a dict is not a vector"),
    ],
)
def test_an_unusable_stored_embedding_is_dropped_rather_than_raising(value, why):
    """A bad vector costs that chunk its re-ranking, never the query.

    These reach a running deployment through a half-migrated corpus, a hand-edited row, or a
    driver that hands back JSON differently -- and the caller is the retrieval path behind every
    management option, so a raise here fails the clinical case.
    """
    assert dense_retrieval.as_vector(value) is None, why


def test_a_chunk_with_an_unusable_embedding_still_retrieves_lexically():
    """The end-to-end shape of the rule above: bad vector, working retrieval."""
    chunk = RetrievableChunk.of(
        GuidelineChunk(
            corpus_version="v1",
            source="icmr",
            document_title="Dengue",
            section_id="D-1",
            heading="Management of dengue",
            content="Maintain hydration in dengue fever.",
            page_range=None,
            keywords=["dengue"],
            embedding=["not", "a", "vector"],
        )
    )

    assert chunk.embedding is None
    assert lexical_score("dengue", [chunk], 5)[0]["section_id"] == "D-1"


# --- cosine --------------------------------------------------------------------------------


def test_cosine_of_a_vector_with_itself_is_one():
    v = dense_retrieval.as_vector([1.0, 2.0, 3.0])

    assert v is not None
    assert math.isclose(dense_retrieval.cosine(v, v), 1.0)


def test_an_opposed_vector_clamps_to_zero_rather_than_going_negative():
    """The re-ranker's contract is a 0..1 position inside a band; a negative similarity would
    push a result below the bottom of its band."""
    a = dense_retrieval.as_vector([1.0, 0.0])
    b = dense_retrieval.as_vector([-1.0, 0.0])

    assert a is not None and b is not None
    assert dense_retrieval.cosine(a, b) == 0.0


def test_vectors_of_different_lengths_score_zero_rather_than_comparing_a_prefix():
    """A length mismatch means the corpus and the query were embedded by different models.

    Comparing their common prefix would produce a number that ranks as though it meant
    something. This is reachable in practice: change ``EMBEDDING_MODEL`` without re-ingesting and
    every stored vector is the old model's.
    """
    assert dense_retrieval.cosine((1.0, 0.0, 0.0), (1.0, 0.0)) == 0.0


# --- availability --------------------------------------------------------------------------


def test_an_empty_query_does_not_load_a_model(monkeypatch):
    """Nothing to embed, so nothing should pay a few hundred MB to find that out."""
    monkeypatch.setattr(
        dense_retrieval, "_load_model", lambda: pytest.fail("model loaded for an empty query")
    )

    assert dense_retrieval.embed_query("   ") is None


def test_dense_retrieval_can_be_switched_off_by_environment(monkeypatch):
    """A memory-boxed deployment running lexical-only is a supported configuration."""
    monkeypatch.setenv("DENSE_RETRIEVAL_DISABLED", "1")
    monkeypatch.setattr(
        dense_retrieval, "_load_model", lambda: pytest.fail("model loaded while disabled")
    )

    assert dense_retrieval.embed_query("management of dengue") is None


def test_a_missing_embedding_library_is_logged_once_and_not_retried(monkeypatch, caplog):
    """The silent-optional-path failure mode, made loud enough to notice.

    R33's vision extraction fell back on every single document for exactly this reason: an
    optional path that failed without saying so is indistinguishable from one that works. It is
    INFO rather than WARNING because running without torch is a real configuration, not a fault.

    Latched because neither failure mode -- no package, or no model cache and no network --
    improves on retry, and retrying would put an import or an HTTP timeout on every retrieval.
    """
    import builtins

    calls = []
    real_import = builtins.__import__

    def _explode(name, *args, **kwargs):
        if name == "sentence_transformers":
            calls.append(name)
            raise ImportError("No module named 'sentence_transformers'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _explode)
    with caplog.at_level("INFO"):
        assert dense_retrieval.embed_query("management of dengue") is None
        assert dense_retrieval.embed_query("management of malaria") is None

    assert len(calls) == 1, "the failed import was retried"
    assert "lexical-only" in caplog.text


def test_a_model_that_will_not_load_degrades_to_lexical_with_a_warning(monkeypatch, caplog):
    """A cache miss with no network reaches here, and must not fail the request."""

    def _boom(*_args, **_kwargs):
        raise OSError("model not found in cache and no network")

    monkeypatch.setattr(dense_retrieval, "_model", None)
    monkeypatch.setitem(
        __import__("sys").modules,
        "sentence_transformers",
        type("m", (), {"SentenceTransformer": _boom}),
    )
    with caplog.at_level("WARNING"):
        assert dense_retrieval.embed_query("management of dengue") is None

    assert "lexical-only" in caplog.text


def test_an_encode_failure_is_not_latched(monkeypatch, caplog):
    """Unlike a load failure, a transient encode error must not drop the process to lexical.

    An OOM or a tokeniser error on one pathological query should cost that query its re-ranking,
    not every query for the life of the worker.
    """

    class _Model:
        def __init__(self):
            self.calls = 0

        def encode(self, _texts, **_kw):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return [[0.6, 0.8]]

    model = _Model()
    monkeypatch.setattr(dense_retrieval, "_load_model", lambda: model)
    with caplog.at_level("WARNING"):
        assert dense_retrieval.embed_query("first") is None

    assert dense_retrieval.embed_query("second") is not None, "one bad encode latched the process"


def test_a_query_that_encodes_to_a_zero_vector_is_dropped(monkeypatch):
    """No direction means no similarity, and normalising would divide by zero."""
    monkeypatch.setattr(
        dense_retrieval,
        "_load_model",
        lambda: type("m", (), {"encode": lambda *_a, **_k: [[0.0, 0.0]]})(),
    )

    assert dense_retrieval.embed_query("management of dengue") is None


# --- dense_scores: the async wiring ----------------------------------------------------------


def _chunk(section_id: str, content: str, keywords: list[str], embedding=None) -> RetrievableChunk:
    return RetrievableChunk.of(
        GuidelineChunk(
            corpus_version="v1",
            source="icmr",
            document_title=f"Doc {section_id}",
            section_id=section_id,
            heading=content[:40],
            content=content,
            page_range=None,
            keywords=keywords,
            embedding=embedding,
        )
    )


@pytest.mark.anyio
async def test_a_corpus_with_no_embeddings_never_loads_a_model(monkeypatch):
    """Ingesting without sentence-transformers is supported, and must not cost a model load
    (or a thread hop) on every retrieval."""
    monkeypatch.setattr(
        dense_retrieval, "embed_query", lambda _t: pytest.fail("embedded an un-embedded corpus")
    )
    chunks = [_chunk("A-1", "dengue fever management", ["dengue"])]

    assert await dense_scores("management of dengue", chunks) is None


@pytest.mark.anyio
async def test_dense_scores_are_keyed_by_source_and_section_and_skip_unembedded(monkeypatch):
    monkeypatch.setattr(dense_retrieval, "embed_query", lambda _t: (1.0, 0.0))
    chunks = [
        _chunk("A-1", "dengue fever management", ["dengue"], embedding=[1.0, 0.0]),
        _chunk("B-1", "malaria management", ["malaria"], embedding=[0.0, 1.0]),
        _chunk("C-1", "hypertension management", ["hypertension"]),
    ]

    scores = await dense_scores("management of dengue", chunks)

    assert scores is not None
    assert math.isclose(scores[("icmr", "A-1")], 1.0)
    assert math.isclose(scores[("icmr", "B-1")], 0.0)
    assert ("icmr", "C-1") not in scores, "a chunk with no vector cannot have a similarity"


@pytest.mark.anyio
async def test_an_unavailable_model_leaves_retrieval_purely_lexical(monkeypatch):
    monkeypatch.setattr(dense_retrieval, "embed_query", lambda _t: None)
    chunks = [_chunk("A-1", "dengue fever management", ["dengue"], embedding=[1.0, 0.0])]

    assert await dense_scores("management of dengue", chunks) is None


@pytest.mark.anyio
async def test_the_query_embedding_is_computed_off_the_event_loop(monkeypatch):
    """A transformer forward pass on the loop that is streaming the Reasoning Theatre would stall
    every other request, the same defect already fixed for bcrypt, blob I/O and the ingest-side
    encode. Asserted by recording the thread the encode actually ran on.
    """
    import threading

    loop_thread = threading.get_ident()
    ran_on: list[int] = []

    def _embed(_text):
        ran_on.append(threading.get_ident())
        return (1.0, 0.0)

    monkeypatch.setattr(dense_retrieval, "embed_query", _embed)
    chunks = [_chunk("A-1", "dengue", ["dengue"], embedding=[1.0, 0.0])]

    await dense_scores("management of dengue", chunks)

    assert ran_on and ran_on[0] != loop_thread, "the embedding ran on the event loop thread"


# --- the invariant: dense reorders, it never re-decides citability ---------------------------


def _banded(scores: list[float]) -> list[tuple[float, RetrievableChunk]]:
    return [(s, _chunk(f"S-{i}", f"content {i}", [])) for i, s in enumerate(scores)]


def test_a_sub_threshold_result_cannot_be_promoted_by_any_similarity():
    """The load-bearing half of the invariant, tested at the extreme a model could actually
    reach: a chunk lexical scored just under the line, and a perfect 1.0 similarity."""
    scored = _banded([0.7499])
    dense = {("icmr", "S-0"): 1.0}

    ((score, _),) = apply_dense_rerank(scored, dense, THRESHOLD)

    assert score < THRESHOLD, "dense similarity made an uncitable guideline citable"


def test_a_citable_result_cannot_be_demoted_by_a_zero_similarity():
    """The other half. A chunk the deterministic retriever vouched for stays citable even if the
    embedding model has never heard of it -- otherwise a model regression silently removes
    guideline grounding the offline path guarantees."""
    scored = _banded([0.75])
    dense = {("icmr", "S-0"): 0.0}

    ((score, _),) = apply_dense_rerank(scored, dense, THRESHOLD)

    assert score >= THRESHOLD, "dense similarity dropped a citable guideline"


def test_the_citable_set_is_identical_under_an_adversarial_similarity_map():
    """The invariant as a property, over the whole shipped corpus and a hostile dense model.

    The adversary here is the realistic failure: a model that is confidently wrong, scoring
    everything lexical rejected at 1.0 and everything it accepted at 0.0. If citability can move
    at all, this moves it.
    """
    from app.services.guideline_ingest import load_corpus

    corpus = [
        RetrievableChunk.of(
            GuidelineChunk(
                corpus_version="bench",
                source=rec["source"],
                document_title=rec["document_title"],
                section_id=rec["section_id"],
                heading=rec["heading"],
                content=rec["content"],
                page_range=rec["page_range"],
                keywords=rec["keywords"],
            )
        )
        for rec in load_corpus()
    ]
    assert corpus, "the shipped guideline corpus no longer loads"
    queries = [
        "management of Dengue fever | high fever for four days with a falling platelet count",
        "management of Hypertension | headache with high bp readings",
        "management of Acute gastroenteritis | loose stools and vomiting for two days",
        "management of Malaria | fever with rigors after travel to a forested district",
    ]

    for query in queries:
        plain = lexical_score(query, corpus, len(corpus))
        citable = {r["section_id"] for r in plain if r["score"] >= THRESHOLD}
        adversarial = {
            (r["source"], r["section_id"]): (0.0 if r["score"] >= THRESHOLD else 1.0) for r in plain
        }

        reranked = lexical_score(query, corpus, len(corpus), dense=adversarial)

        assert {r["section_id"] for r in reranked if r["score"] >= THRESHOLD} == citable, (
            f"{query}: an adversarial dense model changed what may be cited"
        )


def test_dense_similarity_does_reorder_within_the_citable_band():
    """The invariant must not be satisfied by dense doing nothing at all -- reordering the
    citable set is the entire point, because ``guideline_rag`` hands the model its excerpts in
    the order given and the first one is what an option gets grounded in."""
    scored = _banded([0.90, 0.80])
    dense = {("icmr", "S-0"): 0.0, ("icmr", "S-1"): 1.0}

    order = [c.section_id for _, c in apply_dense_rerank(scored, dense, THRESHOLD)]

    assert order == ["S-1", "S-0"]
    assert all(s >= THRESHOLD for s, _ in apply_dense_rerank(scored, dense, THRESHOLD))


def test_a_chunk_with_no_similarity_keeps_its_lexical_score_exactly():
    """A corpus part-way through re-ingestion has embeddings on some chunks and not others, and
    the ones without must not be silently pushed down the ranking."""
    scored = _banded([0.90, 0.80])
    dense = {("icmr", "S-1"): 1.0}

    result = {c.section_id: s for s, c in apply_dense_rerank(scored, dense, THRESHOLD)}

    assert result["S-0"] == 0.90


def test_an_empty_similarity_map_leaves_the_ranking_untouched():
    scored = _banded([0.90, 0.80, 0.20])

    assert apply_dense_rerank(scored, {}, THRESHOLD) == scored


# --- a section_id is only unique within its source -------------------------------------------
#
# The table's unique constraint is (corpus_version, source, section_id) and ``_point_id`` hashes
# all three, so two sources sharing a section_id is legal and expected -- WHO and NICE number
# their sections generically ("1.2.3") where ICMR happens to prefix its ids with the source. The
# similarity map used to key on section_id alone, which silently merged those two chunks: one
# overwrote the other on the way in, and both then re-ranked on whichever survived.


@pytest.mark.anyio
async def test_two_sources_sharing_a_section_id_get_their_own_similarities(monkeypatch):
    monkeypatch.setattr(dense_retrieval, "embed_query", lambda _t: (1.0, 0.0))
    shared = "1.2.3"
    icmr = _chunk(shared, "dengue fever management", ["dengue"], embedding=[1.0, 0.0])
    who = RetrievableChunk.of(
        GuidelineChunk(
            corpus_version="v1",
            source="who",
            document_title="WHO doc",
            section_id=shared,
            heading="malaria",
            content="malaria management",
            page_range=None,
            keywords=["malaria"],
            embedding=[0.0, 1.0],
        )
    )

    scores = await dense_scores("management of dengue", [icmr, who])

    assert scores is not None
    assert math.isclose(scores[("icmr", shared)], 1.0)
    assert math.isclose(scores[("who", shared)], 0.0)


def test_a_shared_section_id_does_not_hand_one_source_the_others_similarity():
    """The consequence of the merge: the WHO chunk is the one the query actually matches, and
    keying on the bare id ranked the ICMR chunk with the WHO chunk's score (or the reverse,
    depending on iteration order) -- an arbitrary result either way."""
    shared = "1.2.3"
    icmr = _chunk(shared, "dengue", [])
    who = RetrievableChunk.of(
        GuidelineChunk(
            corpus_version="v1",
            source="who",
            document_title="WHO doc",
            section_id=shared,
            heading="malaria",
            content="malaria",
            page_range=None,
            keywords=[],
        )
    )
    scored = [(0.90, icmr), (0.80, who)]
    dense = {("icmr", shared): 0.0, ("who", shared): 1.0}

    reranked = apply_dense_rerank(scored, dense, THRESHOLD)

    assert [c.source for _, c in reranked] == ["who", "icmr"]


# --- embed_documents: the corpus-side encode -------------------------------------------------
#
# The write half of the dense path. It is optional in exactly the same ways the read half is,
# and each of those ways has to end in "chunks stored without vectors", never in an exception --
# ``ingest`` runs on the boot path of every deployment.


def test_an_empty_corpus_batch_does_not_load_a_model(monkeypatch):
    """Same bargain as ``embed_query`` on an empty query: nothing to embed, so nothing should
    pay a few hundred MB to discover that.

    This is the ordinary case on a warm deployment, not an edge one -- ``ingest`` is idempotent
    and runs on every boot, so the batch it offers is empty every time the corpus is unchanged.
    """
    monkeypatch.setattr(
        dense_retrieval, "_load_model", lambda: pytest.fail("model loaded for an empty batch")
    )

    assert dense_retrieval.embed_documents([]) is None


def test_corpus_embedding_can_be_switched_off_by_environment(monkeypatch):
    """``DENSE_RETRIEVAL_DISABLED`` has to reach the ingest too. Honouring it only on the query
    side would still load a transformer on every boot -- the cost the switch exists to avoid."""
    monkeypatch.setenv("DENSE_RETRIEVAL_DISABLED", "1")
    monkeypatch.setattr(
        dense_retrieval, "_load_model", lambda: pytest.fail("model loaded while disabled")
    )

    assert dense_retrieval.embed_documents(["hydration guidance"]) is None


def test_a_failed_corpus_encode_is_logged_and_leaves_the_chunks_unembedded(monkeypatch, caplog):
    """An OOM or tokeniser failure part-way through a corpus encode must not abort ingestion:
    the rows still have to land so the deterministic lexical retriever works (Safety Rule #8).

    Logged because a silent optional-path failure is indistinguishable from a working one --
    the same reason R33's vision extraction fell back on every document unnoticed.
    """

    class _Exploding:
        def encode(self, texts, normalize_embeddings=False):
            raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(dense_retrieval, "_load_model", lambda: _Exploding())

    with caplog.at_level("WARNING"):
        assert dense_retrieval.embed_documents(["hydration guidance"]) is None

    assert "CUDA out of memory" in caplog.text


def test_a_thread_that_waited_for_the_lock_reuses_the_model_rather_than_building_another():
    """Why ``_load_model`` holds a lock at all.

    It is reached from ``asyncio.to_thread`` — several concurrent reasoning runs, or a retrieval
    racing an ingest, arrive together on a cold process. Without the re-check inside the lock
    each waiter would build its own copy of the transformer on the way to the same cache entry,
    which is a few hundred MB apiece.
    """
    dense_retrieval.reset()
    holding = threading.Event()
    built: list[str] = []

    class _SlowModel:
        def __init__(self, name: str) -> None:
            built.append(name)
            holding.set()  # the lock is held from here until __init__ returns
            time.sleep(0.3)

    loaded: list[object] = []

    def _load() -> None:
        loaded.append(dense_retrieval._load_model())

    original = sys.modules.get("sentence_transformers")
    sys.modules["sentence_transformers"] = types.SimpleNamespace(SentenceTransformer=_SlowModel)
    try:
        first = threading.Thread(target=_load)
        first.start()
        assert holding.wait(5), "the first thread never entered the model constructor"
        second = threading.Thread(target=_load)  # starts while the lock is held
        second.start()
        first.join(10)
        second.join(10)
    finally:
        if original is None:
            sys.modules.pop("sentence_transformers", None)
        else:
            sys.modules["sentence_transformers"] = original
        dense_retrieval.reset()

    assert built == [dense_retrieval.EMBEDDING_MODEL], f"built {len(built)} models, expected 1"
    assert len(loaded) == 2
    assert loaded[0] is loaded[1] is not None
