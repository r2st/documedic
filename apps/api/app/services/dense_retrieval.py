"""Dense (embedding) retrieval support for the guideline corpus.

This module exists because the dense half of retrieval was built, provisioned, documented — and
never connected. ``guideline_ingest`` embeds every chunk with sentence-transformers, writes the
vector to ``guideline_chunks.embedding``, and upserts it into Qdrant; ``requirements.txt`` pins
``qdrant-client`` and ``sentence-transformers`` (and, transitively, torch); ``docker-compose``
runs a Qdrant service. The read side never touched any of it. ``GuidelineService.retrieve``
called ``lexical_score`` unconditionally, and three module docstrings claimed the opposite —
"Production retrieval is dense vector search in Qdrant ... when they are unavailable the service
falls back to a deterministic lexical retriever". There was no dense path to fall back *from*.

What dense retrieval is allowed to do here is deliberately narrow, and the bound is measured
rather than assumed. See ``guideline_service.apply_dense_rerank`` for the invariant; the
measurement behind it is in ``tests/test_dense_retrieval_quality.py``. In short: on the shipped
corpus, dense similarity ranks the right document first on every benchmark and paraphrase query,
but its absolute scale does not separate "the corpus covers this" from "it does not" — an
off-corpus glaucoma query scores the *diabetes* document at 0.319 while a genuine paraphrased
hypertension query scores its own document at 0.337. So dense evidence reorders what lexical
evidence already made citable, and never decides citability itself.

Both dependencies stay optional and lazily imported: the deterministic lexical retriever is the
offline-safe floor (Critical Safety Rule #8), and a deployment without torch installed must keep
working rather than fail to start.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections.abc import Iterable, Sequence

logger = logging.getLogger(__name__)

# The model ``guideline_ingest._maybe_embed`` writes with. A query embedded by a different model
# is not comparable to the stored chunk vectors, so this is deliberately one constant shared by
# both sides rather than two settings that can drift apart.
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

_model: object | None = None
_load_failed = False
_lock = threading.Lock()


def _env_disabled() -> bool:
    """Whether dense retrieval is switched off by environment.

    Loading a transformer costs a few hundred MB of resident memory and a second or two of
    startup, which a test run or a memory-boxed deployment may not want to pay for a re-ranking
    refinement. ``lexical_score`` alone is the supported configuration, not a broken one.
    """
    return os.getenv("DENSE_RETRIEVAL_DISABLED", "").strip().lower() in {"1", "true", "yes"}


def _load_model() -> object | None:
    """The sentence-transformers model, loaded once per process, or None if unavailable.

    Guarded by a lock because the loader is reached from ``asyncio.to_thread`` — several
    concurrent reasoning runs would otherwise each build their own copy of the model on the way
    to the same cache entry.

    A failure here is remembered (``_load_failed``) rather than retried. The two ways this fails
    are a missing package and a missing model cache with no network, and neither of them gets
    better on the next query; retrying would put a multi-second import or an HTTP timeout on
    every retrieval for the life of the process.
    """
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    with _lock:
        if _model is not None or _load_failed:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:
            # Logged at INFO, not WARNING: running without torch is a supported configuration.
            # It is logged at all because R33's vision-extraction gap was invisible for exactly
            # this reason — an optional path that failed silently read as a working one.
            logger.info(
                "Dense retrieval disabled: sentence-transformers unavailable (%s). "
                "Guideline retrieval is lexical-only.",
                exc,
            )
            _load_failed = True
            return None
        try:
            _model = SentenceTransformer(EMBEDDING_MODEL)
        except Exception as exc:
            logger.warning(
                "Dense retrieval disabled: could not load embedding model %s (%s). "
                "Guideline retrieval is lexical-only.",
                EMBEDDING_MODEL,
                exc,
            )
            _load_failed = True
            return None
        logger.info("Dense retrieval enabled: embedding model %s loaded.", EMBEDDING_MODEL)
        return _model


def reset() -> None:
    """Forget the loaded model and any remembered failure. For tests."""
    global _model, _load_failed
    with _lock:
        _model = None
        _load_failed = False


def embed_query(text: str) -> tuple[float, ...] | None:
    """The query's unit-normalised embedding, or None when dense retrieval is unavailable.

    Synchronous and CPU-bound by nature — callers must keep it off the event loop (see
    ``GuidelineService._dense_scores``), the same discipline ``guideline_ingest`` already applies
    to the corpus-side encode.
    """
    if not text or not text.strip() or _env_disabled():
        return None
    model = _load_model()
    if model is None:
        return None
    try:
        vector = model.encode([text], normalize_embeddings=True)[0]  # type: ignore[attr-defined]
    except Exception as exc:
        # An encode failure is per-query, not per-process, so it is not latched: a transient
        # tokeniser or OOM error should not silently drop the whole deployment to lexical-only.
        logger.warning("Dense query embedding failed (%s); falling back to lexical.", exc)
        return None
    return _unit(float(x) for x in vector)


def _unit(values: Iterable[float]) -> tuple[float, ...] | None:
    """``values`` as a unit vector, or None if it has no length to normalise."""
    vector = tuple(values)
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm or not math.isfinite(norm):
        return None
    return tuple(v / norm for v in vector)


def as_vector(value: object) -> tuple[float, ...] | None:
    """A stored ``guideline_chunks.embedding`` as a unit vector, or None if unusable.

    The column is JSON, written by an optional ingest path and read back by whatever driver the
    deployment uses, so it is validated rather than trusted: a corpus ingested before embeddings
    existed has NULL here, and a hand-edited or half-migrated row can hold anything at all. A bad
    vector costs this chunk its re-ranking, not the query.
    """
    if not isinstance(value, (list | tuple)) or not value:
        return None
    numbers: list[float] = []
    for item in value:
        # bool is an int subclass and is never a legitimate vector component.
        if isinstance(item, bool) or not isinstance(item, (int | float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        numbers.append(number)
    return _unit(numbers)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two already-unit-normalised vectors, clamped to 0..1.

    Both sides are normalised at their source (``embed_query`` and ``as_vector``), so this is a
    dot product. Vectors of different lengths are a model mismatch — a corpus embedded with one
    model and a query with another — and score 0 rather than comparing their common prefix, which
    would be a meaningless number that ranks as if it meant something.

    Negative similarities clamp to 0: they mean "further from this than an unrelated chunk", and
    the re-ranker's contract is a 0..1 position within a band.
    """
    if len(a) != len(b):
        return 0.0
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True))))
