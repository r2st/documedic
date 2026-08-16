"""SBAR handover schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.text_sanitize import clean_free_text, clean_identifier

HandoffStatus = Literal["draft", "sent", "acknowledged"]

# Each SBAR field. The floor is deliberately low and non-zero: a handover with an empty
# Assessment is the failure mode the structure exists to prevent, but a floor high enough to be
# a quality bar would be a floor clinicians pad. Ten characters refuses "-" and "n/a" and
# nothing a clinician actually means.
MIN_SBAR_CHARS = 10
MAX_SBAR_CHARS = 5000
MIN_CLINICIAN_NAME_CHARS = 2
MAX_CLINICIAN_NAME_CHARS = 200
MAX_ACK_NOTE_CHARS = 2000


def _clean_prose(value: str) -> str:
    """Strip control characters, then apply the floor to what is left.

    Applied to the *stripped* text and the stripped form is what gets stored, because
    ``min_length`` counts raw characters: ten spaces satisfied the floor and became ``""`` in
    the column. Exactly the hole that was closed on hard-block override reasoning, and it
    matters here for the same reason — an SBAR field that reads as filled in but holds nothing
    is worse than one that is visibly empty.
    """
    stripped = clean_free_text(value).strip()
    if len(stripped) < MIN_SBAR_CHARS:
        raise ValueError(f"must be at least {MIN_SBAR_CHARS} characters of actual content")
    return stripped


def _clean_name(value: str) -> str:
    stripped = clean_identifier(value).strip()
    if len(stripped) < MIN_CLINICIAN_NAME_CHARS:
        raise ValueError(f"must be at least {MIN_CLINICIAN_NAME_CHARS} characters")
    return stripped


class HandoffCreateRequest(BaseModel):
    """Draft an SBAR handover.

    All four fields are required. That is the whole point of the structure: unstructured verbal
    handover reliably drops the Assessment and the Recommendation, and what survives is a name
    and a diagnosis. A schema that let either be omitted would let them be forgotten in exactly
    the way the format exists to prevent.
    """

    situation: str = Field(..., min_length=MIN_SBAR_CHARS, max_length=MAX_SBAR_CHARS)
    background: str = Field(..., min_length=MIN_SBAR_CHARS, max_length=MAX_SBAR_CHARS)
    assessment: str = Field(..., min_length=MIN_SBAR_CHARS, max_length=MAX_SBAR_CHARS)
    recommendation: str = Field(..., min_length=MIN_SBAR_CHARS, max_length=MAX_SBAR_CHARS)

    @field_validator("situation", "background", "assessment", "recommendation")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_prose(value)


class HandoffUpdateRequest(BaseModel):
    """Edit a draft. Refused once sent."""

    situation: str | None = Field(default=None, max_length=MAX_SBAR_CHARS)
    background: str | None = Field(default=None, max_length=MAX_SBAR_CHARS)
    assessment: str | None = Field(default=None, max_length=MAX_SBAR_CHARS)
    recommendation: str | None = Field(default=None, max_length=MAX_SBAR_CHARS)

    @field_validator("situation", "background", "assessment", "recommendation")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return None if value is None else _clean_prose(value)


class HandoffSendRequest(BaseModel):
    """Hand over: names both clinicians and confirms the chart's outstanding risks.

    ``confirmed_checklist_keys`` must equal the keys the server computes at send time, exactly.
    Not a superset and not a subset: an item that has *gone away* since the draft was written
    means the clinician confirmed a picture that is no longer the current one just as much as a
    new item does, and accepting the superset would wave through a stale confirmation whenever
    the chart happened to improve.

    Read the current list from `GET ../handoffs/checklist` immediately before sending.
    """

    from_clinician: str = Field(
        ...,
        min_length=MIN_CLINICIAN_NAME_CHARS,
        max_length=MAX_CLINICIAN_NAME_CHARS,
        description="Name of the clinician handing over",
    )
    to_clinician: str = Field(
        ...,
        min_length=MIN_CLINICIAN_NAME_CHARS,
        max_length=MAX_CLINICIAN_NAME_CHARS,
        description="Name of the clinician taking over",
    )
    confirmed_checklist_keys: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="Every key from GET ../handoffs/checklist, confirmed as reviewed",
    )

    @field_validator("from_clinician", "to_clinician")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("confirmed_checklist_keys")
    @classmethod
    def _clean_keys(cls, value: list[str]) -> list[str]:
        # Deduplicated here so that sending one key twice is not a mismatch. The comparison in
        # the service is set equality; a client that repeats a key has confirmed it, and failing
        # them for it would be a refusal with nothing clinical behind it.
        return sorted({clean_identifier(key).strip() for key in value if key and key.strip()})


class HandoffAcknowledgeRequest(BaseModel):
    """The receiving clinician's receipt. Closes the loop verbal handover leaves open."""

    acknowledged_by: str = Field(
        ...,
        min_length=MIN_CLINICIAN_NAME_CHARS,
        max_length=MAX_CLINICIAN_NAME_CHARS,
        description="Name of the clinician receiving this patient",
    )
    note: str | None = Field(
        default=None,
        max_length=MAX_ACK_NOTE_CHARS,
        description="Anything the receiving clinician wants recorded back",
    )

    @field_validator("acknowledged_by")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("note")
    @classmethod
    def _clean_note(cls, value: str | None) -> str | None:
        return None if value is None else (clean_free_text(value).strip() or None)


class ChecklistItemResponse(BaseModel):
    key: str
    count: int
    description: str


class HandoffChecklistResponse(BaseModel):
    patient_id: uuid.UUID
    # Only non-zero items ever appear. A checklist that always shows five rows, three of them
    # permanently "nothing to do", is one people learn to tick without reading — and then the
    # two that mattered are ticked the same way.
    items: list[ChecklistItemResponse]
    offline_capable: bool = True


class HandoffResponse(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    status: HandoffStatus
    situation: str
    background: str
    assessment: str
    recommendation: str
    from_clinician: str | None = None
    to_clinician: str | None = None
    # The chart's outstanding risks at the moment of sending — a snapshot, not a live view. The
    # record is of what was handed over, and a checklist that re-derived itself on read would
    # show the *incoming* clinician's chart rather than the outgoing one's.
    checklist: list[ChecklistItemResponse] = Field(default_factory=list)
    sent_at: datetime | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    acknowledgement_note: str | None = None
    created_at: datetime
