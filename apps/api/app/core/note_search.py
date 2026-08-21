"""Relevance ranking for free-text search over clinical notes — pure, and identical on every
database backend.

Why the ranking is here and not in SQL
--------------------------------------
PostgreSQL's ``ts_rank`` is better than anything written in Python, and it is not what this uses.
The reason is that the dev and test databases are SQLite, which has no equivalent, so a
``ts_rank`` ordering would be a behaviour that exists only in production — the one environment
nobody can put a breakpoint in. Every ranking test would be asserting against a code path that
never runs where it is asserted, which is the shape of bug this codebase has already been bitten
by twice (a ``FOR UPDATE`` silently dropped on SQLite, a ``NULLS LAST`` its parser rejects).

So the database does the cheap, indexable part — "which notes contain any of these words" — and
the ordering is computed here, over a bounded candidate set, by a function that behaves the same
everywhere and can be tested directly.

The scoring
-----------
Okapi BM25, with Lucene's always-positive IDF. The saturation matters clinically: a discharge
summary that says "diabetes" eleven times is not eleven times more about diabetes than a clinic
note that says it once, and a linear term-frequency score ranks by document verbosity, which in
a medical record means it ranks by how sick the patient was rather than by what the search was
for.

Two fields, weighted. The presenting complaint is what the visit was *about* and the notes are
what was said about it, so a match in the complaint counts for more — the standard title/body
split, and the reason searching "chest pain" surfaces the chest-pain consultation ahead of the
diabetes review that mentions it in passing.

Folding
-------
Both the query and the document go through the same fold (case, punctuation, a conservative
plural rule). That symmetry is the whole correctness argument: a fold applied to both sides can
only ever make the matcher *more* generous, never less, so an over-eager rule costs precision and
cannot cause a note to be missed. An asymmetric fold — stemming the query but not the document,
which is the usual accident — silently loses results, and a clinical search that silently loses
results is worse than no search at all, because the clinician reads the empty list as an answer.

No markup, ever
---------------
Snippets are returned as text plus integer offsets, never as HTML with the matches wrapped in
tags. Clinical notes are free text a clinician typed; building markup around them here would put
an injection surface in the one part of this API whose whole job is to hand back exactly what
somebody wrote. The client highlights from the offsets.

Pure: no clock, no database, no configuration.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# Terms shorter than this are dropped from a query. Two-letter tokens in a clinical note are
# overwhelmingly units and abbreviations that match everything ("bd", "mg", "od"), and a query
# term that matches every note contributes nothing to an ordering while costing a full scan.
# Digits are exempt — "2" in "type 2" is doing real work.
MIN_TERM_CHARS = 3

# Short tokens exempt from that floor. Two words, and both earn it: "no chest pain" and "chest
# pain" are opposite findings, and a floor that silently eats the "no" turns a search for the
# absence of a symptom into a search for its presence. A quoted phrase already bypasses the
# floor — its tokens are taken whole — so this is only about the loose-term case, which is what
# a clinician actually types.
SHORT_TERMS_KEPT = frozenset({"no", "nil"})

# BM25 term-frequency saturation. 1.2 is the conventional default and there is no corpus here to
# tune it against; a made-up value would be a number nobody could justify later.
BM25_K1 = 1.2
# Length normalisation. 0.75 is the conventional default. It matters more than usual here
# because clinical note lengths are wildly bimodal — a two-line follow-up beside a
# fifteen-paragraph discharge summary — and without it the long one wins every search.
BM25_B = 0.75

# How much a match in the presenting complaint outweighs one in the body of the note.
COMPLAINT_FIELD_WEIGHT = 2.5

# Characters of context around a snippet's match window.
SNIPPET_CONTEXT_CHARS = 60
# The most snippets returned per result. Enough to show that a match is substantive rather than
# incidental; few enough that the response is a search result and not the note itself.
MAX_SNIPPETS_PER_RESULT = 3

# Words dropped from a query as carrying no discrimination. Deliberately tiny and deliberately
# *not* clinical: "no", "not" and "denies" stay, because "no chest pain" and "chest pain" are
# opposite findings and a stop list that eats the negation makes the search answer the wrong
# question.
STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_PHRASE_RE = re.compile(r'"([^"]*)"')


def _fold(token: str) -> str:
    """One token reduced to its comparison form.

    The plural rule is conservative and, more importantly, *symmetric*: it is applied to the
    query and to the document by the same function. "Headaches" and "headache" both reduce to
    "headache"; "diabetes" and "diabetes" both reduce to "diabete", which is not a word and does
    not need to be — it only has to be the same string on both sides.

    Words ending in "ss" ("stress", "abscess") and "us" ("sinus", "bolus") keep their ending:
    stripping there produces a different word rather than a stem, and "stres" would collide with
    nothing while "sinu" would.
    """
    if len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Folded tokens of a piece of text, in order. Punctuation and case are dropped."""
    return [_fold(match.group()) for match in _TOKEN_RE.finditer((text or "").lower())]


@dataclass(frozen=True)
class SearchQuery:
    """A parsed query: loose terms, and quoted phrases that must appear contiguously."""

    terms: tuple[str, ...]
    phrases: tuple[tuple[str, ...], ...]
    # Every distinct token the query needs, phrases included. This is what the database prefilter
    # is built from — a phrase is prefiltered by its words and verified here, because a LIKE on
    # the raw phrase would miss "chest  pain" with two spaces and "chest-pain" with a hyphen,
    # both of which a clinician types and both of which tokenize identically.
    lookup_terms: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.terms and not self.phrases


def parse_query(raw: str) -> SearchQuery:
    """Parse a query string into terms and quoted phrases.

    An unclosed quote is treated as an ordinary character rather than as an error. A clinician
    typing a query is not writing a expression, and refusing ``chest "pain`` with a syntax error
    would be a search box that punishes typing.

    A query that reduces to nothing — all stop words, or all one-character tokens — comes back
    empty, and the caller must refuse rather than return everything. "Every note on the panel"
    is not a plausible answer to a question somebody asked in words.
    """
    lowered = (raw or "").lower()
    phrases: list[tuple[str, ...]] = []
    for match in _PHRASE_RE.finditer(lowered):
        tokens = tuple(tokenize(match.group(1)))
        if tokens:
            phrases.append(tokens)
    remainder = _PHRASE_RE.sub(" ", lowered)
    terms = tuple(
        token
        for token in tokenize(remainder)
        # A digit-bearing token survives the length floor: "t2dm", "2" in "type 2", "b12".
        if (
            len(token) >= MIN_TERM_CHARS
            or any(c.isdigit() for c in token)
            or token in SHORT_TERMS_KEPT
        )
        and token not in STOP_WORDS
    )
    lookup: list[str] = []
    for token in (*terms, *(t for phrase in phrases for t in phrase)):
        if token not in lookup:
            lookup.append(token)
    return SearchQuery(terms=terms, phrases=tuple(phrases), lookup_terms=tuple(lookup))


@dataclass(frozen=True)
class SearchDocument:
    """One encounter's searchable text, as the ranker sees it.

    ``document_id`` is opaque. Everything the ranker needs is in the two fields; the status, the
    date and the supersession are the service's business and never touch the score.
    """

    document_id: str
    presenting_complaint: str | None
    clinician_notes: str | None


@dataclass(frozen=True)
class Snippet:
    """A window of the matched text, plus where the matches are inside it.

    ``field`` says which column the window came from, because "chest pain" appearing as the
    reason for the visit and appearing in a sentence ruling it out are different results.

    ``matches`` are ``(start, end)`` offsets **into ``text``**, not into the original column.
    Returned as offsets rather than as marked-up HTML: see the module docstring.
    """

    field: str
    text: str
    matches: tuple[tuple[int, int], ...]
    truncated_start: bool = False
    truncated_end: bool = False


@dataclass(frozen=True)
class SearchHit:
    document_id: str
    score: float
    snippets: tuple[Snippet, ...] = ()
    matched_terms: tuple[str, ...] = ()
    # True when every quoted phrase in the query was found contiguously. A hit that satisfies
    # the loose terms but not the phrase is still returned — a clinician who quoted a phrase
    # usually still wants the near misses — and this is what lets a client say which is which.
    phrases_matched: bool = False


@dataclass(frozen=True)
class _Prepared:
    document: SearchDocument
    complaint_tokens: list[str] = field(default_factory=list)
    notes_tokens: list[str] = field(default_factory=list)

    @property
    def weighted_length(self) -> float:
        return len(self.complaint_tokens) * COMPLAINT_FIELD_WEIGHT + len(self.notes_tokens)


def _weighted_frequency(prepared: _Prepared, term: str) -> float:
    complaint = prepared.complaint_tokens.count(term) * COMPLAINT_FIELD_WEIGHT
    return complaint + prepared.notes_tokens.count(term)


def _idf(documents_containing: int, total: int) -> float:
    """Lucene's IDF: ``ln(1 + (N - df + 0.5) / (df + 0.5))``.

    Always positive, which the textbook Okapi form is not. That difference decides real results
    here: the candidate set handed to this ranker is *already filtered* to notes containing the
    query's words, so a single-term search has ``df == N`` on every candidate and the textbook
    IDF is negative — every score goes the wrong way round and the least relevant note ranks
    first. A search engine over a whole corpus rarely meets that case; a re-ranker over a
    prefiltered set meets it constantly.
    """
    return math.log(1 + (total - documents_containing + 0.5) / (documents_containing + 0.5))


def _phrase_present(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    if not phrase or len(phrase) > len(tokens):
        return False
    for start in range(len(tokens) - len(phrase) + 1):
        if tuple(tokens[start : start + len(phrase)]) == phrase:
            return True
    return False


def _spans(text: str, wanted: set[str]) -> list[tuple[int, int]]:
    """Character spans in ``text`` whose folded token is one of ``wanted``.

    Computed against the original string so the offsets are usable for highlighting — folding
    changes token lengths, so offsets taken from folded text would drift along the line and
    highlight the wrong words.
    """
    return [
        (match.start(), match.end())
        for match in _TOKEN_RE.finditer(text.lower())
        if _fold(match.group()) in wanted
    ]


def _snippet_for(field_name: str, text: str | None, wanted: set[str]) -> Snippet | None:
    """The densest window of matches in one field, with context around it.

    "Densest" rather than "first": the first mention of a word in a long note is often the
    problem list at the top, and the sentence that actually discusses it is three paragraphs
    down. The window is chosen greedily around the match with the most neighbours inside a
    snippet's width, which is cheap and picks the discussion over the list.
    """
    if not text:
        return None
    spans = _spans(text, wanted)
    if not spans:
        return None

    width = SNIPPET_CONTEXT_CHARS * 2
    best_index, best_count = 0, 0
    for index, (start, _end) in enumerate(spans):
        count = sum(1 for other_start, _ in spans if start <= other_start <= start + width)
        if count > best_count:
            best_index, best_count = index, count

    anchor_start, _anchor_end = spans[best_index]
    window_start = max(0, anchor_start - SNIPPET_CONTEXT_CHARS)
    window_end = min(len(text), anchor_start + width)
    window = text[window_start:window_end]
    inside = tuple(
        (start - window_start, end - window_start)
        for start, end in spans
        if start >= window_start and end <= window_end
    )
    return Snippet(
        field=field_name,
        text=window,
        matches=inside,
        truncated_start=window_start > 0,
        truncated_end=window_end < len(text),
    )


def rank(query: SearchQuery, documents: list[SearchDocument]) -> list[SearchHit]:
    """Score and order ``documents`` against ``query``, most relevant first.

    Documents matching none of the query's terms are dropped rather than returned with a zero
    score: the database prefilter should already have excluded them, and one that slips through
    is a note that has nothing to do with what was asked.

    Ties are left in the caller's order. The service hands documents in newest-first order and
    Python's sort is stable, so two equally relevant notes come back with the recent one first —
    which is the right tie-break in a medical record and costs nothing to get by construction.
    """
    if query.is_empty or not documents:
        return []

    prepared = [
        _Prepared(
            document=document,
            complaint_tokens=tokenize(document.presenting_complaint or ""),
            notes_tokens=tokenize(document.clinician_notes or ""),
        )
        for document in documents
    ]
    total = len(prepared)
    average_length = sum(p.weighted_length for p in prepared) / total or 1.0

    scoring_terms = tuple(query.lookup_terms)
    document_frequency = {
        term: sum(1 for p in prepared if _weighted_frequency(p, term) > 0) for term in scoring_terms
    }

    hits: list[SearchHit] = []
    for p in prepared:
        score = 0.0
        matched: list[str] = []
        for term in scoring_terms:
            frequency = _weighted_frequency(p, term)
            if frequency <= 0:
                continue
            matched.append(term)
            idf = _idf(document_frequency[term], total)
            denominator = frequency + BM25_K1 * (
                1 - BM25_B + BM25_B * (p.weighted_length / average_length)
            )
            score += idf * (frequency * (BM25_K1 + 1)) / denominator
        if not matched:
            continue

        combined = p.complaint_tokens + p.notes_tokens
        phrases_matched = all(
            _phrase_present(p.complaint_tokens, phrase) or _phrase_present(p.notes_tokens, phrase)
            for phrase in query.phrases
        )
        # A quoted phrase found contiguously is a stronger signal than its words scattered
        # across a page, and the multiplier is what stops an exact match losing to a longer note
        # that happens to contain both words far apart. Applied once rather than per phrase:
        # this is a nudge to break a tie, not a second scoring system.
        if query.phrases and phrases_matched:
            score *= 2.0

        wanted = set(matched) | {t for phrase in query.phrases for t in phrase if t in combined}
        snippets = [
            snippet
            for snippet in (
                _snippet_for("presenting_complaint", p.document.presenting_complaint, wanted),
                _snippet_for("clinician_notes", p.document.clinician_notes, wanted),
            )
            if snippet is not None
        ][:MAX_SNIPPETS_PER_RESULT]

        hits.append(
            SearchHit(
                document_id=p.document.document_id,
                score=score,
                snippets=tuple(snippets),
                matched_terms=tuple(matched),
                phrases_matched=phrases_matched,
            )
        )

    return sorted(hits, key=lambda hit: hit.score, reverse=True)
