"""Patient request/response schemas."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.text_sanitize import clean_free_text, clean_identifier

Sex = Literal["male", "female", "other", "unknown"]

# Loose but sane guard against OCR/typo garbage: digits, spaces, and + - ( ) separators only.
_PHONE_RE = re.compile(r"^[0-9+()\-\s]{6,20}$")
# ...and at least this many actual digits. The character-class check alone accepted "++++--()"
# and six spaces, both of which are 6-20 characters of permitted punctuation and neither of
# which is a phone number. Six is the shortest dialable national number, and matches the
# existing 6-character floor.
_MIN_PHONE_DIGITS = 6

# Age-based clinical computations (eGFR, renal dosing thresholds) depend on a plausible DOB.
_MAX_PLAUSIBLE_AGE_YEARS = 130

# Body weight, in kilograms. The bounds match ``ck_patients_weight_kg_plausible`` on the table —
# stated in both places because a 422 naming the field is a better answer to a typo than a 500
# from a constraint violation, and because the constraint has to hold whatever writes to the
# table. Two decimal places is what a paediatric scale reports; more is precision the
# measurement does not have, and Numeric(6, 2) would silently round it away.
_MIN_WEIGHT_KG = 0.1
_MAX_WEIGHT_KG = 650.0
_WEIGHT_DECIMAL_PLACES = 2

# C0/C1 control characters. None of them belong in a name, an address or a phone number, and
# two of them cause real damage downstream rather than just looking odd: U+0000 cannot be
# stored in a PostgreSQL text column at all (asyncpg raises, SQLite accepts it, which is why
# the suite never noticed), and CR/LF turn one field into two in any CSV or plain-text export
# of a chart. Cleaned rather than rejected — see app.core.text_sanitize, which is where the
# rules now live so that every other clinician-writable text field in the API gets them too.


def _validate_dob(value: date | None) -> date | None:
    if value is None:
        return value
    today = date.today()
    if value > today:
        raise ValueError("date_of_birth cannot be in the future")
    # Full elapsed years, not a subtraction of year numbers. The latter called anyone born in a
    # year 131 back "131" regardless of whether the birthday had come round, so a patient who is
    # genuinely 130 -- born late in that year -- had a correct date of birth rejected. It also
    # disagreed with how age is computed everywhere it is actually used (eGFR, renal dosing),
    # which is the reason to state the bound in elapsed years and not in calendar arithmetic.
    age = today.year - value.year - ((today.month, today.day) < (value.month, value.day))
    if age > _MAX_PLAUSIBLE_AGE_YEARS:
        raise ValueError(f"date_of_birth implies an age over {_MAX_PLAUSIBLE_AGE_YEARS} years")
    return value


def _validate_full_name(value: str | None) -> str | None:
    """Reject a name that is not a name; normalise one that is.

    ``min_length=1`` passed "   ", "\\t\\n", and a lone control character, each of which created
    a real chart -- consent recorded, documents attachable, suggestions loggable -- that no
    clinician can identify in a patient list or find by search. There is no recovering from
    that afterwards either: the name is the only human-readable handle on the record.
    """
    if value is None:
        return value
    cleaned = clean_identifier(value)
    if not cleaned:
        raise ValueError(
            "full_name cannot be blank — a chart with no readable name cannot be identified "
            "in the patient list or found by search"
        )
    return cleaned


def _validate_phone(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = clean_identifier(value)
    # A phone field holding only whitespace is an empty phone field. It used to be returned
    # verbatim -- so a chart was stored with phone="      ", which reads as "a number we have"
    # everywhere it is displayed and matches a search for a single space.
    if not cleaned:
        return None
    if not _PHONE_RE.match(cleaned):
        raise ValueError("phone must contain only digits, spaces, and + - ( ) separators")
    if sum(character.isdigit() for character in cleaned) < _MIN_PHONE_DIGITS:
        raise ValueError(f"phone must contain at least {_MIN_PHONE_DIGITS} digits")
    return cleaned


def _validate_free_text(value: str | None) -> str | None:
    """Strip control characters from a multi-line clinical free-text field.

    Unlike an identifier, newlines and tabs are meaningful here — an address has lines, and
    notes are prose — so those survive. Everything else in the C0/C1 range goes, U+0000 above
    all: PostgreSQL cannot store it in a text column, and a note containing one would fail at
    flush and roll back whatever else the request was writing.

    Nothing is rejected and nothing is trimmed beyond that. Clinical free text is the
    clinician's own record and is not the API's to tidy.
    """
    if value is None:
        return value
    # CRLF first, so a Windows-pasted address keeps its line structure rather than losing the
    # break along with the CR.
    return clean_free_text(value)


def _validate_weight(value: float | None) -> float | None:
    """A weight the arithmetic can use, or a 422 saying which end it fell off.

    Weight is a *divisor* — a paediatric dose is milligrams per kilogram per day — so zero and
    negatives are refused rather than stored and worked around at every read. The upper bound is
    a transcription guard: past 650 kg the number is grams entered as kilograms, or a height
    typed into the wrong field.
    """
    if value is None:
        return None
    if not _MIN_WEIGHT_KG <= value <= _MAX_WEIGHT_KG:
        raise ValueError(
            f"weight_kg must be between {_MIN_WEIGHT_KG} and {_MAX_WEIGHT_KG} kilograms"
        )
    return round(value, _WEIGHT_DECIMAL_PLACES)


class PatientCreate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=500)
    date_of_birth: date | None = None
    sex: Sex | None = None
    phone: str | None = Field(default=None, max_length=20)
    address_text: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=10000)
    consent_given: bool = Field(
        ..., description="Must be true before clinical data is stored (DPDP Act)."
    )
    weight_kg: float | None = Field(
        default=None,
        description=(
            "Body weight in kilograms. Read by the deterministic dose-range check, which "
            "cannot judge a child's dose without it."
        ),
    )

    _check_dob = field_validator("date_of_birth")(_validate_dob)
    _check_weight = field_validator("weight_kg")(_validate_weight)
    _check_name = field_validator("full_name")(_validate_full_name)
    _check_phone = field_validator("phone")(_validate_phone)
    _check_free_text = field_validator("address_text", "notes")(_validate_free_text)


class PatientUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=500)
    date_of_birth: date | None = None
    sex: Sex | None = None
    phone: str | None = Field(default=None, max_length=20)
    address_text: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=10000)
    consent_given: bool | None = None
    weight_kg: float | None = Field(
        default=None,
        description=(
            "Body weight in kilograms. Sending it stamps `weight_recorded_at`; sending null "
            "clears both, which is how a weight entered against the wrong chart is undone."
        ),
    )

    _check_dob = field_validator("date_of_birth")(_validate_dob)
    _check_weight = field_validator("weight_kg")(_validate_weight)
    # Same rules on update as on create: an edit that blanks the name leaves exactly the
    # unidentifiable chart that create now refuses to make.
    _check_name = field_validator("full_name")(_validate_full_name)
    _check_phone = field_validator("phone")(_validate_phone)
    _check_free_text = field_validator("address_text", "notes")(_validate_free_text)


class PatientSearchRequest(BaseModel):
    """Search terms for ``POST /patients/search``.

    A POST with a body rather than ``GET ?search=`` on purpose. The search term for this
    endpoint is a patient's name or phone number -- a direct identifier under the DPDP Act,
    and the same value that ``patients.full_name``/``phone`` are encrypted at rest to
    protect. A URL query string is the one part of a request that is logged in cleartext
    almost everywhere by default: nginx's ``$request`` in access.log, cloud load-balancer
    logs, APM traces, browser history, and the ``Referer`` header of any outbound link on
    the resulting page. Putting it in the body keeps it inside the TLS-encrypted payload,
    which nothing on the path logs by default.
    """

    search: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    # Normalised the same way the stored name is, because the two are compared to each other.
    # ``full_name`` is stored with its whitespace runs collapsed, so a term pasted as
    # "Ramesh  Kumar" matched no chart at all until it was collapsed here too.
    @field_validator("search")
    @classmethod
    def _clean_search(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return clean_identifier(value) or None


class PatientResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    date_of_birth: date | None
    sex: str | None
    phone: str | None
    address_text: str | None
    notes: str | None
    consent_given: bool
    consent_given_at: datetime | None
    weight_kg: float | None
    weight_recorded_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PatientSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    date_of_birth: date | None
    sex: str | None
    phone: str | None
    consent_given: bool
    updated_at: datetime
