"""Encounter request/response schemas.

The four bodies here mirror the four things a clinician can do to a visit — open it, edit it
while it is still a draft, sign it, and amend it once it is signed. They are separate models
rather than one partial body because they are not the same act: an edit may leave every field
alone, while an amendment must say what it is changing *and* why, and a signature carries no
clinical content at all.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.text_sanitize import clean_free_text
from app.schemas.common import EncounterStatus

EncounterType = Literal[
    "outpatient", "inpatient", "emergency", "teleconsultation", "follow_up", "other"
]

# Long enough for a consultation note, bounded because the column is unbounded ``Text`` and an
# unbounded body is a memory surface. The per-route body ceiling (app/middleware.py) is the
# other half of that; this is the field-level statement of the same limit.
_NOTES_MAX = 20_000
_COMPLAINT_MAX = 2_000
_REASON_MAX = 2_000

# The floor a documented reason has to clear. One character is not a reason, and an amendment
# whose justification is "." is indistinguishable on read from one with no justification at all
# — the difference being that the schema would have accepted it. Same judgement as the
# hard-block override's documented reasoning.
_REASON_MIN = 10


def _clean_prose(value: str | None) -> str | None:
    """Strip control characters from clinician-written prose, keeping its line structure.

    See app.core.text_sanitize: U+0000 cannot be stored in a PostgreSQL text column at all, so
    a note containing one fails at flush and rolls back the whole request.
    """
    return None if value is None else clean_free_text(value)


def _validate_reason(value: str) -> str:
    """An amendment's reason, cleaned, and refused when cleaning empties it.

    Refused rather than accepted-as-blank because the reason is the entire difference between an
    amendment and an edit. A signed note is preserved and a correction recorded beside it so
    that a reader months later can see what changed and why it changed; a blank reason answers
    the first question and abandons the second, while still spending the one amendment the
    schema allows per visit.
    """
    cleaned = clean_free_text(value).strip()
    if len(cleaned) < _REASON_MIN:
        raise ValueError(
            "amendment_reason must say what is being corrected and why — an amendment to a "
            f"signed note is not recorded without at least {_REASON_MIN} characters of reason"
        )
    return cleaned


def _validate_encounter_date(value: date) -> date:
    """A visit cannot have happened tomorrow.

    The column is the anchor every other row on the chart is ordered against, and a future date
    sorts a visit above everything that has actually happened — so a mistyped year puts an empty
    consultation at the top of a chart the clinician is reading under time pressure.
    """
    if value > date.today():
        raise ValueError("encounter_date cannot be in the future")
    return value


class EncounterCreate(BaseModel):
    """Open a new visit. Lands as a ``draft``; nothing here is attested to until it is signed."""

    encounter_date: date
    encounter_type: EncounterType | None = None
    presenting_complaint: str | None = Field(default=None, max_length=_COMPLAINT_MAX)
    clinician_notes: str | None = Field(default=None, max_length=_NOTES_MAX)

    _check_date = field_validator("encounter_date")(_validate_encounter_date)
    _check_prose = field_validator("presenting_complaint", "clinician_notes")(_clean_prose)


class EncounterUpdate(BaseModel):
    """Edit a visit that is still ``draft`` or ``in_progress``.

    Every field is optional and omitted ones are left alone, so a client can move the status
    without resending the note. ``status`` accepts only the two unsigned states — signing and
    amending are their own endpoints, because each one records a clinical act and writes an
    audit entry that a generic PATCH could not name.
    """

    encounter_date: date | None = None
    encounter_type: EncounterType | None = None
    presenting_complaint: str | None = Field(default=None, max_length=_COMPLAINT_MAX)
    clinician_notes: str | None = Field(default=None, max_length=_NOTES_MAX)
    status: Literal["draft", "in_progress"] | None = None

    _check_date = field_validator("encounter_date")(_validate_encounter_date)
    _check_prose = field_validator("presenting_complaint", "clinician_notes")(_clean_prose)


class EncounterAmend(BaseModel):
    """Open an amendment to a signed visit.

    The result is a *new* draft encounter carrying the corrections, pointing back at the signed
    one. Nothing is changed on the original — not when the amendment is opened, and not when it
    is signed. Fields left out are copied from the encounter being amended, so an amendment that
    only fixes the note does not have to restate the date.
    """

    amendment_reason: str = Field(..., min_length=_REASON_MIN, max_length=_REASON_MAX)
    encounter_date: date | None = None
    encounter_type: EncounterType | None = None
    presenting_complaint: str | None = Field(default=None, max_length=_COMPLAINT_MAX)
    clinician_notes: str | None = Field(default=None, max_length=_NOTES_MAX)

    _check_reason = field_validator("amendment_reason")(_validate_reason)
    _check_date = field_validator("encounter_date")(_validate_encounter_date)
    _check_prose = field_validator("presenting_complaint", "clinician_notes")(_clean_prose)


class EncounterResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patient_id: uuid.UUID
    source_document_id: uuid.UUID | None = None
    encounter_date: date
    encounter_type: EncounterType | None = None
    presenting_complaint: str | None = None
    clinician_notes: str | None = None
    status: EncounterStatus
    signed_at: datetime | None = None
    signed_by_account_id: uuid.UUID | None = None
    amended_at: datetime | None = None
    amends_encounter_id: uuid.UUID | None = None
    amendment_reason: str | None = None
    created_at: datetime
    updated_at: datetime
