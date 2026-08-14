"""Guideline retrieval service (Phase 3, used by the Guideline-RAG agent in Phase 2+).

Retrieval is lexical first and dense second, in that order of authority.

**Lexical** — a deterministic retriever over ``guideline_chunks`` (curated-keyword evidence +
body token overlap, normalised to a 0..1 score) — decides what is *citable*. It is deterministic,
needs no model, and works offline (Critical Safety Rule #8), and every calibration decision in
this module is measured against the shipped corpus by ``tests/test_retrieval_quality_benchmark``.

**Dense** — cosine similarity between the query embedding and the vector ``guideline_ingest``
stored on each chunk — decides only the *order* of what lexical already made citable. It cannot
promote a chunk across the citation threshold in either direction; see ``apply_dense_rerank`` for
the invariant and for the measurement that says it must not.

This docstring used to claim that "production retrieval is dense vector search in Qdrant" with
lexical as an offline fallback. That was never true: ``retrieve`` called ``lexical_score``
unconditionally and nothing in the application read an embedding or queried Qdrant. See
``app.services.dense_retrieval``.

Either way every result keeps its citation metadata (source, section_id, page_range,
corpus_version).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.guideline import GuidelineChunk
from app.services import dense_retrieval

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


# A finding the clinician wrote down as absent. Deliberately small and unambiguous: every cue
# here negates what follows it in plain clinical shorthand, and nothing here has a second
# clinical reading. "rule out"/"ruled out" is *not* a cue -- "rule out ACS" means ACS is being
# actively considered, which is the opposite of absent -- and neither is "normal", which says a
# test was done rather than that a finding is absent.
_NEGATION_CUES = frozenset(
    {
        "no",
        "not",
        "none",
        "never",
        "nil",
        "denies",
        "denied",
        "denying",
        "without",
        "negative",
        "absent",
        "afebrile",
    }
)
# A cue negates to the end of its clause, capped so that one stray "no" cannot erase a long
# clause behind it. Clinical shorthand negates in short runs -- "no chest pain, no fever, no
# bleeding" -- so the clause boundary almost always arrives first.
_NEGATION_SCOPE = 5
_CLAUSE = re.compile(r"[.,;:!?()\[\]|/]+")


def _unnegated(query: str) -> str:
    """The query with every span a cue marks as *absent* removed. See ``query_tokens``."""
    kept: list[str] = []
    for clause in _CLAUSE.split(query or ""):
        remaining = 0
        for word in clause.split():
            token = word.strip("-'\"").lower()
            if token in _NEGATION_CUES:
                remaining = _NEGATION_SCOPE
                continue
            if remaining:
                remaining -= 1
                continue
            kept.append(word)
    return " ".join(kept)


def query_tokens(query: str) -> set[str]:
    """The tokens of a retrieval query, with findings recorded as *absent* left out.

    A guideline query carries the clinician's free-text complaint, and clinical free text
    documents what is not there as carefully as what is: "no chest pain, no breathlessness, no
    fever, no bleeding" is an ordinary line in a note. Scored as plain tokens, every one of those
    words is positive evidence -- "no chest pain" contributed a full keyword hit to *Recognition
    of acute coronary syndrome*, which is precisely backwards, since the clinician had just
    written that the patient does not have it.

    The engine already holds this principle: ``agents.util.text_blob`` deliberately drops the
    keywords of an intake question the clinician answered "no" to, so a screened-out symptom
    cannot trigger a can't-miss match. It was only ever applied to intake answers, and the
    complaint free text -- where clinicians actually write their negatives -- went through
    untouched. Measured against the shipped corpus, an otherwise ordinary note (family history,
    vitals, prior normal investigations, and four documented negatives) pushed 12 of 15 sections
    over the citation threshold; dropping the negated spans removes 5 of them.

    Only the *query* is treated this way. Guideline text states contraindications for a living
    ("NSAIDs should be avoided in dengue"), and a clinician searching for NSAIDs in dengue must
    still find that section -- so chunk body tokens keep their negations.
    """
    return _tokens(_unnegated(query))


# Words that name a section, or a query, as being about *what to do* rather than about what the
# patient has. Matched against a section's heading, section_id and curated keywords, and against
# the query -- see ``is_management_intent``.
_MANAGEMENT_CUES = frozenset(
    {
        "management",
        "managing",
        "manage",
        "mgmt",
        "treatment",
        "treatments",
        "treating",
        "treat",
        "therapy",
        "therapies",
        "therapeutic",
        "pharmacological",
        "pharmacotherapy",
        "prescribe",
        "prescribed",
        "prescribing",
        "regimen",
    }
)


def is_management_intent(text: str) -> bool:
    """Whether this text is about *what to do* — a management section, or a management query.

    Deliberately read from the un-negated text (``_unnegated``) for the same reason scoring is:
    "he takes no regular therapy" is a clinician recording an absence, not a request for therapy
    options. It is *not* read through ``query_tokens``, because "management" is a stopword there
    -- which is the whole problem this exists to solve (see ``_intent_weight``).
    """
    return bool(_MANAGEMENT_CUES & set(_TOKEN.findall(_unnegated(text).lower())))


# A query token counts as a keyword hit if it *is* a keyword word, or is that word give or take
# a short inflectional suffix ("platelet" ~ "platelets", "fever" ~ "fevers"). Both bounds matter:
# below _MIN_STEM_LEN a prefix carries no clinical meaning ("hyp" would hit "hypertension",
# "hypoglycaemia" and "hypothyroidism" alike), and beyond _MAX_STEM_SUFFIX the extra characters
# are a different word rather than an inflection ("cardio" is not "cardiomyopathy").
_MIN_STEM_LEN = 4
_MAX_STEM_SUFFIX = 3


def _keyword_phrases(keywords: Sequence[str]) -> tuple[frozenset[str], ...]:
    """Each curated keyword split into its own words, keeping the phrase boundaries.

    Keywords are curated as phrases ("dengue fever", "acute coronary syndrome"), and the query
    is scored token by token, so the phrase has to be broken up for either to reach the other.
    The split is per keyword rather than corpus-wide because a phrase names *one* concept, and
    ``lexical_score`` has to know how much of that concept a query actually named.
    """
    return tuple(
        words
        for k in keywords
        if (words := frozenset(t for t in _TOKEN.findall(k.lower()) if len(t) > 2))
    )


def _matches_keyword_word(token: str, word: str) -> bool:
    """Whether a query token names a keyword word, exactly or as a short inflection.

    This used to be a substring test — ``any(t in k2 for k2 in keywords)`` — which matched on
    any run of characters anywhere inside a keyword. "ten" hit "hypertension", "art" hit
    "arthritis", "ana" hit "anaemia". Every one of those is a full keyword hit, weighted ×1.5,
    which is enough on its own to push an unrelated guideline section over the retrieval
    threshold and into a management option's citations. Keyword hits have to mean the query
    named the concept, not that its letters happened to appear in the middle of it.
    """
    return token == word or _is_inflection(token, word)


def _is_inflection(a: str, b: str) -> bool:
    """True when one word is the other plus a short suffix (plural, gerund, adverb...)."""
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return (
        len(short) >= _MIN_STEM_LEN
        and len(long) - len(short) <= _MAX_STEM_SUFFIX
        and long.startswith(short)
    )


@dataclass(frozen=True)
class RetrievableChunk:
    """A guideline chunk with its retrieval tokens already computed.

    The lexical retriever scores a query against every chunk in the corpus, and it used to
    tokenise each chunk's heading and body *inside that loop* — so the whole corpus was
    re-tokenised on every query, and the reasoning engine issues one query per hypothesis per
    run. The tokens depend only on the chunk, so they are computed once when the corpus is
    read and reused by every query against it (see ``_load_corpus``).

    Deliberately not a ``GuidelineChunk``. These outlive the session that read them, and a
    detached ORM instance is a lazy-load waiting to happen on a thread with no event loop.
    Field names match the model's so ``GuidelineService._to_dict`` renders either.
    """

    section_id: str
    source: str
    document_title: str
    heading: str | None
    content: str
    page_range: str | None
    corpus_version: str
    keywords: tuple[str, ...]
    body_tokens: frozenset[str]
    # The chunk's stored dense embedding, unit-normalised once here, or None when the corpus was
    # ingested without ``sentence-transformers`` (or with an unusable vector -- see
    # ``dense_retrieval.as_vector``). Carried on the chunk for the same reason the token sets
    # are: the corpus is read and tokenised once per version and reused by every query.
    embedding: tuple[float, ...] | None = None
    # ``keywords`` split into words, once here, for the same reason ``body_tokens`` is: the
    # retriever scores every chunk in the corpus on every query. ``keyword_phrases`` keeps each
    # keyword's words together (what ``lexical_score`` scores against); ``keyword_tokens`` is the
    # flattened union of them, kept because it answers "is this word a keyword word at all".
    keyword_phrases: tuple[frozenset[str], ...] = ()
    keyword_tokens: frozenset[str] = frozenset()
    # Whether this section is about what to *do* about the condition rather than how to
    # recognise it. Derived once here, from the section's own metadata, for the same reason the
    # token sets are: every query is scored against every chunk in the corpus.
    is_management: bool = False

    def __post_init__(self) -> None:
        # Derived, never passed: a directly-constructed chunk (tests, fixtures) would otherwise
        # score with no keywords at all and silently lose every keyword hit.
        if not self.keyword_phrases and self.keywords:
            object.__setattr__(self, "keyword_phrases", _keyword_phrases(self.keywords))
        if not self.keyword_tokens and self.keyword_phrases:
            object.__setattr__(
                self,
                "keyword_tokens",
                frozenset(t for words in self.keyword_phrases for t in words),
            )
        if not self.is_management:
            # Heading first (it is what a curator writes the section's purpose into), then the
            # section_id, whose "-MGMT" convention survives a re-worded heading, then the
            # curated keywords. Never the body: guideline prose discusses treatment inside a
            # diagnosis section all the time, and that does not make it a management section.
            object.__setattr__(
                self,
                "is_management",
                is_management_intent(
                    f"{self.heading or ''} {self.section_id} {' '.join(self.keywords)}"
                ),
            )

    @classmethod
    def of(cls, chunk: GuidelineChunk) -> RetrievableChunk:
        return cls(
            section_id=chunk.section_id,
            source=chunk.source,
            document_title=chunk.document_title,
            heading=chunk.heading,
            content=chunk.content,
            page_range=chunk.page_range,
            corpus_version=chunk.corpus_version,
            keywords=tuple(str(x).lower() for x in (chunk.keywords or [])),
            body_tokens=frozenset(_tokens(f"{chunk.heading or ''} {chunk.content}")),
            embedding=dense_retrieval.as_vector(chunk.embedding),
        )


_KEYWORD_WEIGHT = 1.5


def _keyword_weight(q_tokens: set[str], phrases: tuple[frozenset[str], ...]) -> float:
    """How much keyword evidence a query supplies, counted a keyword at a time.

    Each matched keyword word is worth ×1.5, as before — discounted by how much of its own
    keyword the query actually named. A keyword is a phrase naming one concept, and matching one
    word of "acute coronary syndrome" is not naming that concept.

    This is the same rule as ``_matches_keyword_word``, one level up. That rule stopped a token
    matching part of a *word* ("ten" inside "hypertension"); this one stops a token matching part
    of a *phrase*, which had the same effect and the same cause. Every word of a phrase scored a
    full, independent keyword hit, so the generic qualifier that clinical phrases tend to start
    with certified a match on its own. Measured against the shipped corpus: "acute" — from the
    hypothesis name "Acute gastroenteritis" — scored both acute-coronary-syndrome sections at
    0.625 on a query with nothing cardiac in it, and "high", from "high blood pressure", scored
    the hypertension section on any query that mentioned a high fever.

    Recall is untouched where it was earned, by construction rather than by measurement: when the
    query names every word of a phrase the discount is 1, so the phrase contributes exactly what
    it always did, and a single-word keyword ("dengue", "metformin") is a one-word phrase that
    can only ever be matched in full. Only a partial match loses weight, and it loses it in
    proportion to how partial it is — "dengue" against "dengue fever" is half a phrase and keeps
    half of one hit, still enough to rank that section first, because nothing else in the corpus
    matches "dengue" at all.
    """
    total = 0.0
    for words in phrases:
        matched = sum(1 for w in words if any(_matches_keyword_word(t, w) for t in q_tokens))
        if matched:
            total += _KEYWORD_WEIGHT * matched * (matched / len(words))
    return total


def _normalise(raw: float) -> float:
    """Saturating relevance: independent of query length so a long multi-diagnosis query is not
    penalised. raw>=4.5 (≈3 keyword hits, or 2 hits + body overlap) clears the 0.75 threshold."""
    return round(raw / (raw + _KEYWORD_WEIGHT), 4) if raw else 0.0


def _document_key(chunk: RetrievableChunk) -> tuple[str, str]:
    return (chunk.source, chunk.document_title)


def _chunk_key(chunk: RetrievableChunk) -> tuple[str, str]:
    """What identifies one chunk: its source *and* its section id, never the section id alone.

    A ``section_id`` is only unique within its source. That is what the table enforces (unique on
    ``corpus_version, source, section_id``), what ``guideline_ingest._point_id`` hashes, and what
    ``_document_key`` assumes one level up. The shipped 15-chunk corpus happens to prefix every id
    with its source (``ICMR-HTN-DX``), so nothing collides today -- but WHO and NICE sections are
    numbered generically (``1.2.3``) in the real documents this corpus grows into, and two sources
    landing on one id is then ordinary rather than exotic.
    """
    return (chunk.source, chunk.section_id)


# What a matched intent is worth, in the same units as a keyword hit (×1.5) -- so roughly one and
# a third keyword hits. Calibrated against the shipped corpus as the smallest weight that carries
# every benchmark case's own management section over the citation threshold; see
# ``_intent_weight`` for why over-shooting it cannot cost precision.
_INTENT_WEIGHT = 2.0


def _intent_weight(
    query_is_management: bool,
    chunk: RetrievableChunk,
    raw: float,
    confirmed_documents: frozenset[tuple[str, str]],
) -> float:
    """Evidence that this section answers the *kind* of question the query is asking.

    ``guideline_rag`` asks one question and only one: "management of {leading diagnoses} |
    {complaint}". The retriever could not hear the "management" part of it. "management" is a
    stopword in ``_STOP`` -- it has to be, because it is also a curated keyword on nearly every
    management section, so scoring it as an ordinary token lifted every management section in the
    corpus at once -- and dropping it left the query with nothing but the diagnosis names and the
    presenting complaint. Both of those are *symptom* vocabulary, and symptom vocabulary is what
    diagnosis sections are keyed on ("headache", "chest pain", "cough", "polyuria"). Management
    sections are keyed on therapy vocabulary ("amlodipine", "artemisinin", "ORS") that a query
    built from diagnoses and a complaint never contains.

    So the ranking within the right document came out backwards, systematically. Measured on the
    shipped corpus across the seven benchmark cases, the diagnosis section outscored the
    management section in *every* document that has both, and three cases -- dengue,
    hypertension, malaria -- could not put their own management section over the citation
    threshold at all. ``guideline_rag`` then grounded its management options in diagnosis text:
    "Guidelines support considering: Diagnosis and staging of hypertension...". That is R34's
    known limitation, and it is a retrieval defect rather than a threshold that is set too high.

    The intent match is real evidence and is scored as such, under two gates:

      * **the section must already have lexical evidence for this query** (``raw > 0``). Being a
        management section is not on its own a reason to cite one.
      * **the section's document must already be citable on topical evidence alone.** This is the
        gate that makes the whole thing safe: promotion happens strictly *inside* a document some
        section of which already cleared the threshold without any intent credit. No document can
        become citable that was not already, so citation precision -- which the benchmark judges
        per document, because any section of the right condition is a defensible grounding and no
        section of a different condition is -- cannot fall, by construction rather than by
        measurement. The weight above is therefore a recall knob only.
    """
    if not query_is_management or raw <= 0 or not chunk.is_management:
        return 0.0
    return _INTENT_WEIGHT if _document_key(chunk) in confirmed_documents else 0.0


# How much of a result's position *within its band* dense similarity decides. 0 is lexical order
# untouched, 1 is pure dense order. Only ever a ranking knob: ``apply_dense_rerank`` cannot move a
# result across the citation threshold at any value of this, so it has no bearing on which
# guideline a management option may cite -- only on which excerpt the model reads first.
_DENSE_RERANK_WEIGHT = 0.5

# Scores are rounded to 4dp, so this is one representable step below the citation threshold: what
# a sub-threshold result is capped at so re-ranking cannot lift it over the line.
_SCORE_EPSILON = 0.0001


def apply_dense_rerank(
    scored: list[tuple[float, RetrievableChunk]],
    dense: Mapping[tuple[str, str], float],
    cut: float,
) -> list[tuple[float, RetrievableChunk]]:
    """Reorder results by dense similarity *within* the citable and non-citable bands.

    The invariant, which is what makes dense retrieval safe to switch on at all: **a result that
    lexical evidence placed below the citation threshold stays below it, and one placed at or
    above it stays above.** That is enforced structurally rather than by tuning -- each result is
    re-positioned inside its own band, and the sub-threshold band is capped one representable step
    under ``cut`` -- so no value of ``_DENSE_RERANK_WEIGHT``, and no embedding model however
    miscalibrated, can make a guideline citable that the deterministic retriever did not already
    make citable.

    That bound is not caution for its own sake; it is what the measurement supports. Dense
    similarity ranks the right document first on all seven benchmark cases *and* on all five
    paraphrased ones, where the lexical retriever cites nothing at all -- so it clearly knows
    something lexical does not. But its absolute scale does not separate "the corpus covers this
    condition" from "it does not". Measured on the shipped corpus: the weakest genuine paraphrase
    match (hypertension, "giddiness with bp running high") scores 0.337 against its own document,
    while an off-corpus glaucoma query scores 0.319 against the *diabetes* document and an
    off-corpus hepatitis B query scores 0.299 against *malaria*. There is no cut between them.
    Simulated end to end, every dense weight large enough to recover one paraphrase case also
    produced at least one off-corpus citation -- a management option for glaucoma grounded in the
    diabetes guideline. In a CDSS that is the precise failure the citation threshold exists to
    prevent, and recall bought with it is not a trade worth making.

    What is left is real and measurable: within the citable set, dense order puts the *management*
    section first on 3 of the 7 benchmark cases against 1 for lexical order alone. ``guideline_rag``
    asks a management question and hands the model its excerpts in the order given, so that is the
    excerpt the option gets grounded in.

    Closing the paraphrase gap needs first-stage dense retrieval over a corpus large enough for
    absolute similarity to mean something -- the full ICMR spine rather than the 15 chunks that
    ship today -- and a re-measurement of the separation above. Until then a paraphrased query
    fails *safe*: it cites nothing and ``guideline_rag`` reports insufficient guideline support,
    rather than citing the wrong condition.
    """
    if not dense:
        return scored
    reranked: list[tuple[float, RetrievableChunk]] = []
    for score, chunk in scored:
        similarity = dense.get(_chunk_key(chunk))
        if similarity is None:
            reranked.append((score, chunk))
            continue
        low, high = (cut, 1.0) if score >= cut else (0.0, cut)
        span = high - low
        # Where lexical evidence put this result inside its own band, then nudged toward where
        # dense evidence would put it. Both terms are in 0..1, so the blend is too, and the
        # result cannot leave the band it came from.
        position = (score - low) / span if span else 0.0
        blended = (1 - _DENSE_RERANK_WEIGHT) * position + _DENSE_RERANK_WEIGHT * similarity
        adjusted = round(low + blended * span, 4)
        if score < cut:
            adjusted = min(adjusted, round(cut - _SCORE_EPSILON, 4))
        else:
            adjusted = max(adjusted, cut)
        reranked.append((adjusted, chunk))
    reranked.sort(key=lambda x: x[0], reverse=True)
    return reranked


def lexical_score(
    query: str,
    chunks: Sequence[RetrievableChunk],
    k: int,
    *,
    threshold: float | None = None,
    dense: Mapping[tuple[str, str], float] | None = None,
) -> list[dict]:
    """Deterministic lexical retriever: keyword evidence (×1.5) + body token overlap, normalised.

    Shared by the async ``GuidelineService.retrieve`` and the sync retriever closure the
    ReasoningContext needs (it has no event loop / DB access).

    Scored in two passes. The first is topical only, and decides which documents the query is
    about; the second adds intent evidence within those documents (``_intent_weight``).
    ``threshold`` is the citation threshold the caller will apply to the result -- it is what
    "this document is about the case" means -- and defaults to the one the reasoning engine uses.

    ``dense`` maps ``(source, section_id)`` to the query's cosine similarity with that chunk, when
    a dense model is available. It reorders results inside their band and never changes which of
    them clear ``threshold`` -- see ``apply_dense_rerank``. The name of this function is therefore
    still accurate: what it *retrieves* is lexical, always.
    """
    q_tokens = query_tokens(query)
    if not q_tokens or not chunks:
        return []
    cut = settings.guideline_retrieval_threshold if threshold is None else threshold

    # Chunks with no overlap at all are dropped here rather than carried through both passes:
    # zero cannot clear the threshold, and it cannot be promoted either (``_intent_weight``
    # requires existing evidence), so nothing downstream would ever look at them. On a corpus the
    # size of the full ICMR spine that is most of it, on most queries.
    topical = [
        (chunk, raw)
        for chunk in chunks
        if (
            raw := _keyword_weight(q_tokens, chunk.keyword_phrases)
            + len(q_tokens & chunk.body_tokens)
        )
    ]
    confirmed = frozenset(_document_key(c) for c, raw in topical if _normalise(raw) >= cut)

    query_is_management = is_management_intent(query)
    scored: list[tuple[float, RetrievableChunk]] = []
    for chunk, raw in topical:
        score = _normalise(raw + _intent_weight(query_is_management, chunk, raw, confirmed))
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda x: x[0], reverse=True)
    if dense:
        scored = apply_dense_rerank(scored, dense, cut)
    return [GuidelineService._to_dict(c, s) for s, c in scored[:k]]


async def dense_scores(
    query: str, chunks: Sequence[RetrievableChunk]
) -> dict[tuple[str, str], float] | None:
    """Cosine similarity of ``query`` against every chunk that carries an embedding.

    None when dense retrieval is unavailable — no model installed, the corpus was ingested
    without embeddings, or the query would not encode. Every one of those is an ordinary
    configuration rather than an error, and each leaves retrieval exactly as lexical as it was.

    The encode is CPU-bound (a transformer forward pass) and runs on a worker thread for the same
    reason ``guideline_ingest`` off-loads the corpus-side encode and ``security`` off-loads
    bcrypt: this is called from request handlers and from the reasoning engine, on the event loop
    that is serving every other request at the same time.
    """
    if not any(c.embedding for c in chunks):
        return None
    vector = await asyncio.to_thread(dense_retrieval.embed_query, query)
    if vector is None:
        return None
    return {
        _chunk_key(c): dense_retrieval.cosine(vector, c.embedding)
        for c in chunks
        if c.embedding is not None
    }


# corpus_version -> (stamp, chunks). See GuidelineService._load_corpus for why this is safe.
_CORPUS_CACHE: dict[str, tuple[tuple[int, datetime | None], tuple[RetrievableChunk, ...]]] = {}

# The corpus is versioned reference data, so in practice one entry is live at a time and a
# second appears only across an ingestion. The cap is a leak-stopper, not a tuning knob.
_MAX_CACHED_CORPUS_VERSIONS = 3


def reset_corpus_cache() -> None:
    """Drop every cached corpus. For tests that rebuild the schema underneath the process."""
    _CORPUS_CACHE.clear()


class GuidelineService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _load_corpus(self, corpus_version: str | None) -> tuple[RetrievableChunk, ...]:
        """The corpus for a version, tokenised, cached per process.

        Without the cache this is the most expensive read in the application. Every retrieval
        pulled every chunk of the corpus — full guideline text, thousands of rows once the ICMR
        spine is loaded — into ORM instances, and ``ReasoningService._build_context`` does it on
        *every* reasoning request, including the intake rounds that never retrieve anything.

        Caching it is safe because of what this data is: a curated corpus, published under a
        version, identical for every patient and every account. There is nothing per-request in
        it to go stale, and nothing patient-specific to leak between requests.

        The cache is still validated rather than trusted, because ingestion appends to a live
        version and a stale corpus means a management option cites a guideline the corpus no
        longer says. The check is one indexed aggregate — row count and latest ``updated_at`` —
        against the alternative of transferring and instantiating the whole corpus. It sees
        appends, edits, and deletes; it would miss a delete and an insert of exactly the same
        number of rows all timestamped no later than what was already cached, which append-only
        versioned reference data does not do.
        """
        version = corpus_version or settings.guideline_corpus_version
        stamp_row = (
            await self.db.execute(
                select(func.count(), func.max(GuidelineChunk.updated_at)).where(
                    GuidelineChunk.corpus_version == version
                )
            )
        ).one()
        stamp = (int(stamp_row[0] or 0), stamp_row[1])

        cached = _CORPUS_CACHE.get(version)
        if cached is not None and cached[0] == stamp:
            return cached[1]

        result = await self.db.execute(
            select(GuidelineChunk).where(GuidelineChunk.corpus_version == version)
        )
        chunks = tuple(RetrievableChunk.of(c) for c in result.scalars().all())
        if len(_CORPUS_CACHE) >= _MAX_CACHED_CORPUS_VERSIONS:
            _CORPUS_CACHE.clear()
        _CORPUS_CACHE[version] = (stamp, chunks)
        return chunks

    async def retrieve(
        self, query: str, k: int = 6, *, corpus_version: str | None = None
    ) -> list[dict]:
        """Return up to ``k`` chunk dicts with a 0..1 ``score``.

        Lexical evidence decides what is citable; dense similarity, when available, decides the
        order within that (see ``apply_dense_rerank``).
        """
        chunks = await self._load_corpus(corpus_version)
        return lexical_score(query, chunks, k, dense=await dense_scores(query, chunks))

    @staticmethod
    def _to_dict(chunk: GuidelineChunk | RetrievableChunk, score: float) -> dict:
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

    async def get_by_section_ids(
        self, section_ids: list[str], *, corpus_version: str | None = None
    ) -> list[dict]:
        """Fetch specific chunks by section_id, deterministically (no relevance scoring).

        Used by the clinical-pathway feature, which cites known section ids rather than
        lexical/dense search, so callers always get the exact grounding chunk rather than
        whatever a query happens to rank highest.
        """
        if not section_ids:
            return []
        version = corpus_version or settings.guideline_corpus_version
        result = await self.db.execute(
            select(GuidelineChunk).where(
                GuidelineChunk.corpus_version == version,
                GuidelineChunk.section_id.in_(section_ids),
            )
        )
        chunks = {c.section_id: c for c in result.scalars().all()}
        # Preserve caller order; silently skip ids the corpus doesn't (yet) have -- pathway
        # stage text stands on its own even without a live citation attached.
        return [self._to_dict(chunks[sid], 1.0) for sid in section_ids if sid in chunks]

    async def count(self, corpus_version: str | None = None) -> int:
        """How many chunks the corpus holds — counted in SQL.

        This used to load every chunk, with its full guideline text, to call ``len()`` on the
        list. It backs an unauthenticated status endpoint, so the corpus was transferred and
        instantiated once per caller who wanted a number.
        """
        version = corpus_version or settings.guideline_corpus_version
        total = await self.db.scalar(
            select(func.count())
            .select_from(GuidelineChunk)
            .where(GuidelineChunk.corpus_version == version)
        )
        return int(total or 0)
