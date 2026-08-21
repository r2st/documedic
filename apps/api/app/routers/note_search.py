"""Full-text search across the calling account's clinical notes.

**Why this is a POST.** A search query is clinical content about the person being searched for.
"HIV", "termination", "self-harm" — typed into a query string, each of those is written into
nginx's access log, into the browser's history, and into any proxy between the two, none of
which are places this deployment's data-retention or residency promises reach. The same argument
already keeps the real access token out of a URL (see ``app.core.security.create_stream_token``).
So the query travels in a request body, and the route is a POST that reads rather than writes —
which is the right trade here and is stated so nobody "fixes" it into a GET later.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.openapi import AUTH_ERRORS
from app.schemas.note_search import (
    NoteSearchHitResponse,
    NoteSearchRequest,
    NoteSearchResponse,
    NoteSnippetResponse,
)
from app.services.note_search_service import NoteSearchService

router = APIRouter(prefix="/notes", tags=["notes"])


@router.post(
    "/search",
    response_model=NoteSearchResponse,
    summary="Search clinical notes across the panel",
    # No patient in the path, so no 404: an account with no matching notes gets an empty result
    # list rather than a not-found. A patient_id in the body that belongs to somebody else
    # matches nothing, for the same reason — the account join is applied as well as it, never
    # instead of it.
    responses=AUTH_ERRORS,
)
async def search_notes(
    body: NoteSearchRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> NoteSearchResponse:
    """Relevance-ranked search over presenting complaints and clinician notes.

    Ranked with BM25 over the notes containing the query's words. A match in the presenting
    complaint counts for more than one in the body — the visit that was *about* chest pain ranks
    ahead of the diabetes review that mentions it in passing — and term frequency saturates, so
    a fifteen-page discharge summary does not outrank a two-line note by being longer.

    Quoted substrings are phrases: `"chest pain"` requires the words adjacent. Near misses are
    still returned, flagged with `phrases_matched: false`.

    **`superseded_by` must be rendered.** A result may be a version of a visit that has since
    been amended, and the strongest textual match is quite often exactly that — a statement a
    clinician corrected. It is returned rather than hidden, because the words really are on the
    record, and it is marked so it cannot be read as current.

    `truncated` means more notes matched than one search ranks, oldest dropped first.

    Audited as `clinical_notes_searched` — counts only. The query itself is never recorded: a
    permanent, unencrypted note of what a clinician was looking for, scoped to one chart, names
    a patient and a suspicion in the same row.
    """
    report = await NoteSearchService(db).search(
        account_id=account.id,
        query=body.query,
        patient_id=body.patient_id,
        limit=body.limit,
    )
    await db.commit()
    return NoteSearchResponse(
        query=body.query,
        results=[
            NoteSearchHitResponse(
                encounter_id=result.encounter_id,
                patient_id=result.patient_id,
                encounter_date=result.encounter_date,
                encounter_type=result.encounter_type,
                status=result.status,
                score=result.hit.score,
                matched_terms=list(result.hit.matched_terms),
                phrases_matched=result.hit.phrases_matched,
                snippets=[
                    NoteSnippetResponse(
                        field=snippet.field,
                        text=snippet.text,
                        matches=[(start, end) for start, end in snippet.matches],
                        truncated_start=snippet.truncated_start,
                        truncated_end=snippet.truncated_end,
                    )
                    for snippet in result.hit.snippets
                ],
                superseded_by=result.superseded_by,
            )
            for result in report.results
        ],
        total_candidates=report.total_candidates,
        truncated=report.truncated,
    )
