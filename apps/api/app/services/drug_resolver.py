"""Resolve raw drug names to DrugVocabulary entries (brand -> generic -> reference_id).

Never matches by string equality alone (CLAUDE.md pitfall #4): resolution goes through the
vocabulary, with exact brand/generic match first, then trigram/Levenshtein fuzzy matching
for OCR misspellings (e.g. "Crocin" -> Paracetamol, "Glycomet" -> Metformin).

Two resolution strategies, chosen per lookup:

* **Exact** (``resolve_reference_id``, and the first tier of ``resolve``) is answered by a
  targeted, bounded query. These are equality lookups on indexed columns, so the database
  can serve them without the whole corpus crossing the wire.
* **Fuzzy** needs every candidate name in memory, because rapidfuzz scores the query against
  all of them. That path loads the active vocabulary once per resolver and reuses it.

The distinction matters because the vocabulary is the one reference table that grows with the
product's market coverage rather than with the patient: this project's corpus is Indian brand
names, of which there are tens of thousands, and resolution runs for every medication and
every allergy of every safety check. Loading all of it to answer "which row has reference_id
DRUG-0042?" cost a full table read plus ORM hydration of every row on the hot deterministic
safety path — measured at 12ms -> 19ms for one ``active_flags`` call when 2000 unrelated
drugs were added to a 50-row corpus, i.e. growing with the corpus and not with the patient.

Both strategies memoise per resolver instance, so repeated lookups of the same name or
reference id within one request still cost no SQL at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.drug_vocabulary import DrugVocabulary

# Minimum fuzzy score (0-100) to accept a non-exact match.
FUZZY_THRESHOLD = 86.0

# Alphanumeric runs. Splitting on everything else is what makes "Crocin + Augmentin",
# "Warfarin, Aspirin" and "Crocin\nAugmentin" all tokenize the same way, so no single
# separator has to be enumerated.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(text.lower()))


@dataclass(frozen=True)
class ResolvedDrug:
    vocabulary_id: object
    reference_id: str
    generic_name: str
    brand_name: str | None
    drug_class: str | None
    match_type: str  # exact_brand | exact_generic | exact_reference | fuzzy
    score: float
    # Carried through so a medication linked only by name still reaches the safety engine with
    # its curated liver-injury tier. Without it the hepatotoxic burden of a chart would depend
    # on which of its rows happened to have a vocabulary id.
    hepatotoxicity: str | None = None


# Exact-match tiers in precedence order: a reference id is the canonical key, and a brand name
# beats a generic one (real vocabularies collide -- a brand marketed under another drug's INN --
# and the precedence is what stops the collision silently changing which product resolves).
_EXACT_TIERS: tuple[tuple[str, str], ...] = (
    ("reference_id", "exact_reference"),
    ("brand_name", "exact_brand"),
    ("generic_name", "exact_generic"),
)


@dataclass(frozen=True)
class _FuzzyIndex:
    """Candidate names for fuzzy matching, built once per resolver from the active vocabulary.

    Only the fuzzy tier needs this. rapidfuzz scores the query against every candidate, so
    there is no way to push that work into the database — the corpus has to be in memory.
    Exact lookups are served by targeted queries and never build it, which is what keeps the
    hot safety path off a full-vocabulary read (see the module docstring).

    First-row-wins on duplicate names, which is exactly ``dict.setdefault``, so the candidate
    map is assembled in one pass at construction rather than by re-scanning per lookup.
    """

    rows: list[DrugVocabulary]
    candidates: dict[str, DrugVocabulary]  # brand + generic names, lower-cased
    keys: list[str]  # rapidfuzz wants a sequence; materialised once
    # First token of a candidate name -> that name's full token sequence and its row. Lets
    # `rows_named_in` consider only the candidates that could start at each position in the
    # query, instead of testing the whole corpus name by name.
    by_first_token: dict[str, list[tuple[tuple[str, ...], DrugVocabulary]]] = field(
        default_factory=dict
    )

    @classmethod
    def build(cls, rows: list[DrugVocabulary]) -> _FuzzyIndex:
        candidates: dict[str, DrugVocabulary] = {}
        for row in rows:
            if row.brand_name:
                candidates.setdefault(row.brand_name.lower(), row)
            candidates.setdefault(row.generic_name.lower(), row)
        by_first_token: dict[str, list[tuple[tuple[str, ...], DrugVocabulary]]] = {}
        for name, row in candidates.items():
            if tokens := _tokens(name):
                by_first_token.setdefault(tokens[0], []).append((tokens, row))
        return cls(
            rows=rows,
            candidates=candidates,
            keys=list(candidates.keys()),
            by_first_token=by_first_token,
        )

    def rows_named_in(self, query: str) -> dict[str, DrugVocabulary]:
        """Distinct vocabulary rows whose name appears *whole* inside ``query``.

        Keyed by reference id. Matching is on whole tokens (a candidate's token sequence
        appearing contiguously in the query's), not on raw substrings: "Crocin" must not be
        found inside "Crocinex", and "Amoxicillin + Clavulanic acid" must match across whatever
        punctuation separates its words. A typo like "Crocine" matches nothing here and is left
        to the fuzzy tier, which is the whole point — this answers "which drugs did this text
        *name*", not "what did it probably mean".

        Keying by reference id means a query naming a brand and its own generic ("Crocin
        (Paracetamol) 500") yields the one drug it is, while two different molecules yield two.
        Colliding rows that share a generic name are already collapsed by ``candidates`` (first
        row wins), so "Aspirin" resolving against both ASP-75 and ASP-150 appears once.

        Whole rows rather than names, because the two callers need different parts of them:
        counting the drugs in a clinician's proposal needs the generic name to read back, while
        screening a guideline management option against a patient's chart needs the
        ``drug_class`` the allergy cross-reactivity check keys on.
        """
        query_tokens = _tokens(query)
        found: dict[str, DrugVocabulary] = {}
        for position, token in enumerate(query_tokens):
            for candidate_tokens, row in self.by_first_token.get(token, ()):
                span = query_tokens[position : position + len(candidate_tokens)]
                if span == candidate_tokens:
                    found[row.reference_id] = row
        return found

    def drugs_named_in(self, query: str) -> dict[str, str]:
        """:meth:`rows_named_in` reduced to reference id -> generic name.

        The form the ambiguity check at the clinician-proposal boundary wants: it needs to name
        the drugs back to the clinician, not to evaluate rules against them.
        """
        return {ref: row.generic_name for ref, row in self.rows_named_in(query).items()}


class DrugResolver:
    """Vocabulary lookups for one unit of work. Not safe to share across requests.

    The memo caches assume the vocabulary does not change underneath the instance, which holds
    for a request but not for a long-lived object: reference data is reloaded by seeding.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._rows: list[DrugVocabulary] | None = None
        self._index: _FuzzyIndex | None = None
        self._by_name: dict[str, ResolvedDrug | None] = {}
        self._by_reference_id: dict[str, DrugVocabulary | None] = {}
        # Exact-match rows per case-folded query string. An empty list is a cached *negative* —
        # "no exact match, go straight to fuzzy" — which is what stops a batch of unresolvable
        # names from re-issuing one exact query each.
        self._exact: dict[str, list[DrugVocabulary]] = {}

    async def _all(self) -> list[DrugVocabulary]:
        """The one place the whole vocabulary is read. Kept as its own seam so tests can stub it.

        Only the fuzzy tier reaches here — see the module docstring.
        """
        if self._rows is None:
            result = await self.db.execute(
                select(DrugVocabulary).where(DrugVocabulary.is_active.is_(True))
            )
            self._rows = list(result.scalars().all())
        return self._rows

    async def _load(self) -> _FuzzyIndex:
        if self._index is None:
            self._index = _FuzzyIndex.build(await self._all())
        return self._index

    async def _exact_rows(self, query: str) -> list[DrugVocabulary]:
        """Active rows whose reference id, brand, or generic equals ``query`` (case-folded).

        Deliberately unbounded. The result is every row in one drug's brand family — the rows
        sharing a generic name, which is what "Paracetamol" matches in an Indian vocabulary —
        so it is bounded by how many brands carry that one molecule, not by the corpus.
        """
        if query in self._exact:
            return self._exact[query]
        result = await self.db.execute(self._exact_statement([query]))
        rows = list(result.scalars().all())
        self._exact[query] = rows
        return rows

    @staticmethod
    def _exact_statement(queries: list[str]) -> Select[tuple[DrugVocabulary]]:
        """Active rows matching any of ``queries`` on reference id, brand, or generic name.

        Deliberately unordered, which preserves the tie-break this resolver has always had.
        Name collisions are real in the seeded corpus — "Aspirin" is the generic of both
        ASP-75 and ASP-150 — and only one of the colliding rows carries the curated
        interaction rules, so *which* row wins decides whether a drug interaction fires. That
        made the previous behaviour ("first row the full-corpus scan returned") load-bearing
        clinical behaviour, and this query reproduces it by returning the same rows in the
        same natural order.

        Imposing an explicit total order here instead — ``(created_at, reference_id)`` was
        tried — silently flips which product a colliding generic resolves to, and with it
        whether the warfarin/aspirin interaction is raised. The fix for that fragility is in
        the reference data (colliding rows should share a reference id, or every strength
        should carry the same rules), not in this ordering.
        """
        return select(DrugVocabulary).where(
            DrugVocabulary.is_active.is_(True),
            or_(
                func.lower(DrugVocabulary.reference_id).in_(queries),
                func.lower(DrugVocabulary.brand_name).in_(queries),
                func.lower(DrugVocabulary.generic_name).in_(queries),
            ),
        )

    @classmethod
    def _pick(cls, rows: list[DrugVocabulary], query: str) -> tuple[DrugVocabulary, str] | None:
        """The one row ``query`` resolves to, by tier precedence then by row order.

        ``rows`` must be in ``_exact_statement`` order, so the first row of the
        highest-precedence tier present is the answer — see that method on why the order is
        the table's natural one.
        """
        for attribute, match_type in _EXACT_TIERS:
            for row in rows:
                value = getattr(row, attribute)
                if value and value.lower() == query:
                    return row, match_type
        return None

    async def prefetch(self, names: Iterable[str | None]) -> None:
        """Warm the exact-match memo for a batch of names with a single query.

        Callers that already know every name they are about to resolve — merging a whole
        prescription, building a safety context from a medication list — should call this
        first. Without it each ``resolve`` issues its own exact-match query, which is an N+1
        that grows with the size of the document or the patient's medication list.

        Names that have no exact match are memoised as such, so a batch of unrecognised names
        costs one query here and then one shared corpus load on the fuzzy path, not one query
        each.
        """
        queries = sorted(
            {name.strip().lower() for name in names if name and name.strip()} - self._exact.keys()
        )
        if not queries:
            return
        result = await self.db.execute(self._exact_statement(queries))
        grouped: dict[str, list[DrugVocabulary]] = {query: [] for query in queries}
        # Rows arrive in `_exact_statement` order, so appending preserves it within each group
        # and `_pick` sees exactly what the single-name path would have seen.
        for row in result.scalars().all():
            for value in (row.reference_id, row.brand_name, row.generic_name):
                if value and (folded := value.lower()) in grouped:
                    # One row can match a name on two tiers; it is still one candidate row.
                    if row not in grouped[folded]:
                        grouped[folded].append(row)
        self._exact.update(grouped)

    async def resolve(self, name: str | None) -> ResolvedDrug | None:
        """Resolve a raw drug name (brand, generic, or reference id) through the vocabulary.

        Returns ``None`` rather than guessing when nothing clears the fuzzy threshold — an
        unresolved drug is excluded from safety evaluation, so a wrong guess is worse than no
        answer (CLAUDE.md pitfall #4).
        """
        if not name or not name.strip():
            return None
        query = name.strip().lower()
        if query in self._by_name:
            return self._by_name[query]
        resolved = await self._resolve_uncached(query)
        self._by_name[query] = resolved
        return resolved

    async def _resolve_uncached(self, query: str) -> ResolvedDrug | None:
        # 1) Exact match, answered by one targeted query over this name's candidate rows.
        matched = self._pick(await self._exact_rows(query), query)
        if matched is not None:
            return self._make(matched[0], matched[1], 100.0)

        # 2) Fuzzy match against brand and generic names (OCR misspellings). Needs every
        #    candidate in memory, so this is the one tier that reads the whole corpus.
        index = await self._load()
        if not index.keys:
            return None
        best = process.extractOne(query, index.keys, scorer=fuzz.WRatio)
        if best and best[1] >= FUZZY_THRESHOLD:
            # rapidfuzz returns (matched_key, score, position); the candidate map is keyed by
            # the matched *name*, not by the score or the position.
            return self._make(index.candidates[best[0]], "fuzzy", float(best[1]))
        return None

    async def drugs_named_in(self, name: str | None) -> dict[str, str]:
        """Which distinct vocabulary drugs ``name`` names, as reference id -> generic name.

        Exists so a caller can tell "I don't recognise this" apart from "you named more than one
        drug" — two failures that need opposite advice from the clinician. See
        :meth:`_FuzzyIndex.rows_named_in` for the matching rule.

        Note what this deliberately is *not*: it is not consulted by :meth:`resolve`. Resolution
        also runs over names already in the patient's record, where an Indian combination product
        stored as one row ("Amlodipine + Atenolol") resolving to its first molecule is a partial
        safety check, but refusing to resolve it at all would drop it from the evaluation
        entirely — see ``SafetyService._current_meds``, which silently skips what it cannot
        resolve. Ambiguity is refused at the boundary where a clinician proposes a drug and gets
        a verdict back about that input, not everywhere a stored name is looked up.
        """
        if not name or not name.strip():
            return {}
        return (await self._load()).drugs_named_in(name.strip().lower())

    async def rows_named_in(self, text: str | None) -> dict[str, DrugVocabulary]:
        """The vocabulary rows ``text`` names, as reference id -> row.

        The form :meth:`drugs_named_in` reduces; see :meth:`_FuzzyIndex.rows_named_in` for the
        matching rule and for why the whole row is worth carrying.
        """
        if not text or not text.strip():
            return {}
        return (await self._load()).rows_named_in(text.strip().lower())

    async def resolve_reference_id(self, reference_id: str) -> DrugVocabulary | None:
        """The active vocabulary row with this exact reference id, or ``None``.

        ``reference_id`` is unique across the table, so this is a single-row index lookup and
        needs none of the corpus the fuzzy path loads.
        """
        if reference_id in self._by_reference_id:
            return self._by_reference_id[reference_id]
        result = await self.db.execute(
            select(DrugVocabulary).where(
                DrugVocabulary.reference_id == reference_id,
                DrugVocabulary.is_active.is_(True),
            )
        )
        row = result.scalars().first()
        self._by_reference_id[reference_id] = row
        return row

    async def resolve_reference_ids(
        self, reference_ids: Iterable[str]
    ) -> dict[str, DrugVocabulary]:
        """Batch form of :meth:`resolve_reference_id`: one query for the whole set.

        ``active_flags`` re-evaluates every current medication, which is a reference-id lookup
        per drug. Issuing them one at a time is an N+1 that grows with the patient's
        medication list; this collapses them into a single ``IN`` query and populates the
        per-instance memo so a later single lookup is free.
        """
        wanted = {ref for ref in reference_ids if ref}
        missing = wanted - self._by_reference_id.keys()
        if missing:
            result = await self.db.execute(
                select(DrugVocabulary).where(
                    DrugVocabulary.reference_id.in_(missing),
                    DrugVocabulary.is_active.is_(True),
                )
            )
            found = {row.reference_id: row for row in result.scalars().all()}
            for ref in missing:
                self._by_reference_id[ref] = found.get(ref)
        return {ref: row for ref in wanted if (row := self._by_reference_id.get(ref)) is not None}

    @staticmethod
    def _make(row: DrugVocabulary, match_type: str, score: float) -> ResolvedDrug:
        return ResolvedDrug(
            vocabulary_id=row.id,
            reference_id=row.reference_id,
            generic_name=row.generic_name,
            brand_name=row.brand_name,
            drug_class=row.drug_class,
            match_type=match_type,
            score=score,
            hepatotoxicity=row.hepatotoxicity,
        )
