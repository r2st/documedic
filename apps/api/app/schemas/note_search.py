"""Clinical-notes search request/response schemas."""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, Field, field_validator

from app.core.text_sanitize import clean_free_text
from app.services.note_search_service import MAX_RESULTS

# The longest query accepted. Generous for a search box and far short of a payload: a "query"
# of ten thousand characters is not a question, and every one of its terms costs a LIKE across
# the panel's notes.
MAX_QUERY_CHARS = 200
MIN_QUERY_CHARS = 2


class NoteSnippetResponse(BaseModel):
    """A window of matched text and where the matches sit inside it."""

    field: str = Field(
        description=(
            "`presenting_complaint` or `clinician_notes` — a term appearing as the reason for "
            "the visit and appearing in a sentence ruling it out are different results."
        )
    )
    text: str
    matches: list[tuple[int, int]] = Field(
        default_factory=list,
        description=(
            "Character offsets into `text`, for the client to highlight. Offsets rather than "
            "marked-up HTML: this text is what a clinician typed, and building markup around "
            "it server-side would put an injection surface in the one response whose job is to "
            "hand back exactly what somebody wrote."
        ),
    )
    truncated_start: bool = False
    truncated_end: bool = False


class NoteSearchHitResponse(BaseModel):
    """One matching visit."""

    encounter_id: uuid.UUID
    patient_id: uuid.UUID
    encounter_date: date
    encounter_type: str | None = None
    status: str
    score: float = Field(
        description=(
            "Relevance, for ordering only. Not comparable between searches — it is a BM25 score "
            "computed against this query over this candidate set."
        )
    )
    matched_terms: list[str] = Field(default_factory=list)
    phrases_matched: bool = Field(
        default=False,
        description=(
            "Whether every quoted phrase was found contiguously. Near misses are still "
            "returned; this is what lets a client separate them."
        ),
    )
    snippets: list[NoteSnippetResponse] = Field(default_factory=list)
    superseded_by: uuid.UUID | None = Field(
        default=None,
        description=(
            "Set when this visit has been amended, naming the encounter that replaced it. "
            "**Render it.** A perfect textual match may be a version of a note a clinician has "
            "since corrected, and presenting it unmarked hands back a retracted statement in "
            "answer to a clinical question."
        ),
    )


class NoteSearchResponse(BaseModel):
    query: str = Field(description="The query as submitted, echoed for display")
    results: list[NoteSearchHitResponse] = Field(default_factory=list)
    total_candidates: int = Field(
        description="Notes containing any of the query's words that were ranked"
    )
    truncated: bool = Field(
        description=(
            "True when more notes matched than one search ranks. The oldest are the ones "
            "dropped, so a search that finds nothing on a truncated read has not searched the "
            "whole record."
        )
    )


class NoteSearchRequest(BaseModel):
    """Search terms. Quoted substrings are treated as phrases that must appear contiguously."""

    query: str = Field(
        ...,
        min_length=MIN_QUERY_CHARS,
        max_length=MAX_QUERY_CHARS,
        description=(
            "Words to search for. Wrap a phrase in double quotes to require it contiguously: "
            '`"chest pain"` will not match a note that says "chest" and "pain" a page apart.'
        ),
    )
    patient_id: uuid.UUID | None = Field(
        default=None, description="Narrow the search to one chart. Omit to search the whole panel."
    )
    limit: int = Field(
        default=20, ge=1, le=MAX_RESULTS, description="Most relevant results to return"
    )

    @field_validator("query")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = clean_free_text(value).strip()
        if len(cleaned) < MIN_QUERY_CHARS:
            raise ValueError(f"must be at least {MIN_QUERY_CHARS} characters")
        return cleaned
