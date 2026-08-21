"""Full-text search across a panel's clinical notes.

Two halves, deliberately split. The database finds *candidates* — notes containing any of the
query's words, scoped to the calling account, bounded — and ``app.core.note_search`` decides the
order. Why the ordering is not in SQL is argued in that module; what is here is the retrieval,
the tenancy, and the one clinical judgement this feature has to get right.

That judgement is supersession. Searching a record turns up the note that best matches the
words, and the note that best matches the words may be a version of a visit that a clinician has
since corrected. "Penicillin — tolerated" is a perfect match for a search about penicillin, and
if it was amended last year to say the opposite, presenting it as a search result with no mark
on it is this system handing a clinician a retracted statement in answer to a safety question.
Amended encounters are therefore returned — hiding them would be its own kind of lie, since the
words really are on the record — and every one of them carries ``superseded_by``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.note_search import SearchDocument, SearchHit, parse_query, rank
from app.models.encounter import Encounter
from app.models.patient import Patient
from app.services.audit_service import AuditService

# The most notes one search will rank. The ranking is O(candidates × terms) in Python, and this
# is the ceiling on both the query and that loop.
MAX_CANDIDATES = 500

# The most results returned to a caller in one page.
MAX_RESULTS = 50

# LIKE's own wildcards, plus the escape character itself. A query containing "%" would otherwise
# be a full scan matching every note on the panel — and "50%" is a thing clinicians write.
_LIKE_ESCAPE = "\\"


def _escape_like(term: str) -> str:
    escaped = term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
    for wildcard in ("%", "_"):
        escaped = escaped.replace(wildcard, _LIKE_ESCAPE + wildcard)
    return escaped


@dataclass(frozen=True)
class NoteSearchResult:
    """One matching visit: the hit's ranking output plus the chart context to render it in."""

    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    hit: SearchHit
    encounter_date: date
    encounter_type: str | None
    status: str
    # The encounter that superseded this one, when this one has been amended. See the module
    # docstring — this is the field that keeps a corrected note from being read as current.
    superseded_by: uuid.UUID | None = None


@dataclass(frozen=True)
class NoteSearchReport:
    results: list[NoteSearchResult]
    total_candidates: int
    truncated: bool


class NoteSearchService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def search(
        self,
        *,
        account_id: uuid.UUID,
        query: str,
        patient_id: uuid.UUID | None = None,
        limit: int = 20,
        audit: bool = True,
    ) -> NoteSearchReport:
        """Rank this account's clinical notes against ``query``.

        Scoped by joining ``encounters`` to ``patients`` on the owning account rather than by
        collecting the account's patient ids first and filtering on them. The join cannot be
        got wrong by a later edit in the way an ``IN (...)`` of ids can — a forgotten filter
        there is a search across the whole deployment's notes, which is the worst single failure
        this endpoint could have.

        ``patient_id`` narrows to one chart. It is applied *as well as* the account join, never
        instead of it: a patient id from another account's panel must match nothing rather than
        match everything on that chart.

        An empty parsed query returns nothing. The caller refuses it before reaching here; this
        is the second of the two places, because "every note on the panel" is not an answer to a
        question somebody asked in words.
        """
        parsed = parse_query(query)
        if parsed.is_empty:
            return NoteSearchReport(results=[], total_candidates=0, truncated=False)

        conditions = []
        for term in parsed.lookup_terms:
            pattern = f"%{_escape_like(term)}%"
            conditions.append(Encounter.presenting_complaint.ilike(pattern, escape=_LIKE_ESCAPE))
            conditions.append(Encounter.clinician_notes.ilike(pattern, escape=_LIKE_ESCAPE))

        statement = (
            select(Encounter)
            .join(Patient, Patient.id == Encounter.patient_id)
            .where(
                Patient.account_id == account_id,
                Patient.is_deleted.is_(False),
                Encounter.is_deleted.is_(False),
                or_(*conditions),
            )
        )
        if patient_id is not None:
            statement = statement.where(Encounter.patient_id == patient_id)
        # Newest first, so that when the candidate cap bites it drops the oldest notes, and so
        # that the stable sort in ``rank`` breaks score ties towards the recent visit. The
        # primary key is the final key for the reason every paged read in this codebase carries
        # one: without a total order, a cap is a lottery over ties rather than a partition.
        statement = statement.order_by(Encounter.encounter_date.desc(), Encounter.id.desc()).limit(
            MAX_CANDIDATES + 1
        )

        rows = list((await self.db.execute(statement)).scalars().all())
        truncated = len(rows) > MAX_CANDIDATES
        candidates = rows[:MAX_CANDIDATES]

        hits = rank(
            parsed,
            [
                SearchDocument(
                    document_id=str(row.id),
                    presenting_complaint=row.presenting_complaint,
                    clinician_notes=row.clinician_notes,
                )
                for row in candidates
            ],
        )
        by_id = {str(row.id): row for row in candidates}
        capped = hits[: min(limit, MAX_RESULTS)]
        superseded = await self._successors(
            [
                uuid.UUID(hit.document_id)
                for hit in capped
                if by_id[hit.document_id].status == "amended"
            ]
        )

        results = [
            NoteSearchResult(
                encounter_id=by_id[hit.document_id].id,
                patient_id=by_id[hit.document_id].patient_id,
                hit=hit,
                encounter_date=by_id[hit.document_id].encounter_date,
                encounter_type=by_id[hit.document_id].encounter_type,
                status=by_id[hit.document_id].status,
                superseded_by=superseded.get(by_id[hit.document_id].id),
            )
            for hit in capped
        ]

        if audit:
            await self.audit.record(
                action="clinical_notes_searched",
                account_id=account_id,
                patient_id=patient_id,
                entity_type="encounter",
                entity_id=None,
                # Counts and shape only. The *query* is the one thing that must never land
                # here: a clinician searching "HIV" across their panel writes a permanent,
                # unencrypted record of what they were looking for, and where the search was
                # scoped to one chart that record names a patient and a suspicion together.
                payload={
                    "term_count": len(parsed.lookup_terms),
                    "phrase_count": len(parsed.phrases),
                    "candidates": len(candidates),
                    "results": len(results),
                    "scoped_to_patient": patient_id is not None,
                    "truncated": truncated,
                },
            )

        return NoteSearchReport(
            results=results, total_candidates=len(candidates), truncated=truncated
        )

    async def _successors(self, amended_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
        """``{amended encounter id: the signed amendment that replaced it}``.

        One query for the whole page rather than one per result — the N+1 this codebase has
        already had to remove from two other reads. Only signed amendments count:
        ``uq_encounters_one_signed_amendment`` admits one of those per original, and a *draft*
        amendment is somebody partway through a correction, which has not superseded anything
        yet.
        """
        if not amended_ids:
            return {}
        rows = (
            (
                await self.db.execute(
                    select(Encounter.amends_encounter_id, Encounter.id).where(
                        Encounter.amends_encounter_id.in_(amended_ids),
                        Encounter.status.in_(("signed", "amended")),
                        Encounter.is_deleted.is_(False),
                    )
                )
            )
            .tuples()
            .all()
        )
        return {original: successor for original, successor in rows if original is not None}
