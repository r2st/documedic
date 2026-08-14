"""Guideline retrieval service (Phase 3, used by the Guideline-RAG agent in Phase 2+).

Production retrieval is dense vector search in Qdrant; ``qdrant-client`` and the embedding model
are optional and imported lazily. When they are unavailable the service falls back to a
deterministic lexical retriever over ``guideline_chunks`` (keyword + token overlap, normalised to
a 0..1 score) so retrieval and citation work offline and in tests. Either way every result keeps
its citation metadata (source, section_id, page_range, corpus_version).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
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
    return _tokens(" ".join(kept))


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
    # ``keywords`` split into words, once here, for the same reason ``body_tokens`` is: the
    # retriever scores every chunk in the corpus on every query. ``keyword_phrases`` keeps each
    # keyword's words together (what ``lexical_score`` scores against); ``keyword_tokens`` is the
    # flattened union of them, kept because it answers "is this word a keyword word at all".
    keyword_phrases: tuple[frozenset[str], ...] = ()
    keyword_tokens: frozenset[str] = frozenset()

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


def lexical_score(query: str, chunks: Sequence[RetrievableChunk], k: int) -> list[dict]:
    """Deterministic lexical retriever: keyword evidence (×1.5) + body token overlap, normalised.

    Shared by the async ``GuidelineService.retrieve`` and the sync retriever closure the
    ReasoningContext needs (it has no event loop / DB access).
    """
    q_tokens = query_tokens(query)
    if not q_tokens or not chunks:
        return []
    scored: list[tuple[float, RetrievableChunk]] = []
    for chunk in chunks:
        raw = _keyword_weight(q_tokens, chunk.keyword_phrases) + len(q_tokens & chunk.body_tokens)
        # Saturating relevance: independent of query length so a long multi-diagnosis query
        # is not penalised. raw>=4.5 (≈3 keyword hits, or 2 hits + body overlap) clears 0.75.
        score = round(raw / (raw + _KEYWORD_WEIGHT), 4) if raw else 0.0
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [GuidelineService._to_dict(c, s) for s, c in scored[:k]]


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
        """Return up to ``k`` chunk dicts with a 0..1 ``score`` (lexical fallback retriever)."""
        return lexical_score(query, await self._load_corpus(corpus_version), k)

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
