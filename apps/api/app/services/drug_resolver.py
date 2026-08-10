"""Resolve raw drug names to DrugVocabulary entries (brand -> generic -> reference_id).

Never matches by string equality alone (CLAUDE.md pitfall #4): resolution goes through the
vocabulary, with exact brand/generic match first, then trigram/Levenshtein fuzzy matching
for OCR misspellings (e.g. "Crocin" -> Paracetamol, "Glycomet" -> Metformin).
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.drug_vocabulary import DrugVocabulary

# Minimum fuzzy score (0-100) to accept a non-exact match.
FUZZY_THRESHOLD = 86.0


@dataclass(frozen=True)
class ResolvedDrug:
    vocabulary_id: object
    reference_id: str
    generic_name: str
    brand_name: str | None
    drug_class: str | None
    match_type: str  # exact_brand | exact_generic | exact_reference | fuzzy
    score: float


@dataclass(frozen=True)
class _VocabularyIndex:
    """Lookup tables built once per resolver from the active vocabulary.

    Resolution used to linear-scan the whole vocabulary up to four times per call (once per
    exact-match tier, then again to assemble fuzzy candidates). That is fine at a few dozen
    rows and quadratic-feeling at a realistic Indian brand corpus, on a path that runs for
    every medication and every allergy of every safety check. The scans are all
    first-row-wins, which is exactly ``dict.setdefault`` — so they become one pass, at
    construction, reused across calls.
    """

    rows: list[DrugVocabulary]
    by_reference_id: dict[str, DrugVocabulary]  # exact, case-sensitive
    by_reference_id_lower: dict[str, DrugVocabulary]
    by_brand: dict[str, DrugVocabulary]
    by_generic: dict[str, DrugVocabulary]
    fuzzy_candidates: dict[str, DrugVocabulary]  # brand + generic names, lower-cased
    fuzzy_keys: list[str]  # rapidfuzz wants a sequence; materialised once

    @classmethod
    def build(cls, rows: list[DrugVocabulary]) -> _VocabularyIndex:
        by_reference_id: dict[str, DrugVocabulary] = {}
        by_reference_id_lower: dict[str, DrugVocabulary] = {}
        by_brand: dict[str, DrugVocabulary] = {}
        by_generic: dict[str, DrugVocabulary] = {}
        fuzzy_candidates: dict[str, DrugVocabulary] = {}
        for row in rows:
            by_reference_id.setdefault(row.reference_id, row)
            by_reference_id_lower.setdefault(row.reference_id.lower(), row)
            if row.brand_name:
                by_brand.setdefault(row.brand_name.lower(), row)
                fuzzy_candidates.setdefault(row.brand_name.lower(), row)
            by_generic.setdefault(row.generic_name.lower(), row)
            fuzzy_candidates.setdefault(row.generic_name.lower(), row)
        return cls(
            rows=rows,
            by_reference_id=by_reference_id,
            by_reference_id_lower=by_reference_id_lower,
            by_brand=by_brand,
            by_generic=by_generic,
            fuzzy_candidates=fuzzy_candidates,
            fuzzy_keys=list(fuzzy_candidates.keys()),
        )


class DrugResolver:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._rows: list[DrugVocabulary] | None = None
        self._index: _VocabularyIndex | None = None

    async def _all(self) -> list[DrugVocabulary]:
        """The one place the vocabulary is read. Kept as its own seam so tests can stub it."""
        if self._rows is None:
            result = await self.db.execute(
                select(DrugVocabulary).where(DrugVocabulary.is_active.is_(True))
            )
            self._rows = list(result.scalars().all())
        return self._rows

    async def _load(self) -> _VocabularyIndex:
        if self._index is None:
            self._index = _VocabularyIndex.build(await self._all())
        return self._index

    async def resolve(self, name: str | None) -> ResolvedDrug | None:
        if not name or not name.strip():
            return None
        query = name.strip().lower()
        index = await self._load()

        # 1) Exact matches, in precedence order: reference_id, brand, generic.
        for table, match_type in (
            (index.by_reference_id_lower, "exact_reference"),
            (index.by_brand, "exact_brand"),
            (index.by_generic, "exact_generic"),
        ):
            row = table.get(query)
            if row is not None:
                return self._make(row, match_type, 100.0)

        # 2) Fuzzy match against brand and generic names (OCR misspellings).
        if not index.fuzzy_keys:
            return None
        best = process.extractOne(query, index.fuzzy_keys, scorer=fuzz.WRatio)
        if best and best[1] >= FUZZY_THRESHOLD:
            return self._make(index.fuzzy_candidates[best[0]], "fuzzy", float(best[1]))
        return None

    async def resolve_reference_id(self, reference_id: str) -> DrugVocabulary | None:
        return (await self._load()).by_reference_id.get(reference_id)

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
        )
