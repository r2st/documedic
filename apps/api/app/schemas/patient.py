"""Patient request/response schemas."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

# C0/C1 control characters. None of them belong in a name, an address or a phone number, and
# two of them cause real damage downstream rather than just looking odd: U+0000 cannot be
# stored in a PostgreSQL text column at all (asyncpg raises, SQLite accepts it, which is why
# the suite never noticed), and CR/LF turn one field into two in any CSV or plain-text export
# of a chart. Cleaned rather than rejected -- see _clean_identifier.
# The C0/C1 range excluding \t \n \v \f \r, which Python's ``\s`` already matches and which
# _WHITESPACE_RUN therefore folds into a single space. Splitting the range this way is what
# keeps "Ramesh\tKumar" two words: deleting the whole control range would join them into one
# unsearchable token, the very defect this normalisation exists to prevent.
_NON_SPACING_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")
# For multi-line free text, where tab and newline are content rather than separators.
_CONTROL_CHARS_KEEPING_LINES = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_WHITESPACE_RUN = re.compile(r"\s+")


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


def _clean_identifier(value: str) -> str:
    """Normalise a single-line identifying field: no control characters, no stray whitespace.

    Cleaning rather than rejecting, deliberately. A control character or a doubled space in a
    pasted name is not a clinical error and not something the clinician can usefully be asked to
    find; refusing the whole chart creation over one would be a worse outcome than quietly
    fixing it. What *is* rejected is a value that turns out to be empty once cleaned, because
    that is not a recoverable typo — see :func:`_validate_full_name`.

    Whitespace runs collapse because patient search is a substring match over the decrypted
    name (``PatientService.list``): a chart stored as "Ramesh  Kumar" is invisible to a search
    for "Ramesh Kumar", which for an encrypted-at-rest name is the only way to find it.

    Only the non-spacing control characters are deleted; tab, newline and friends are left for
    the whitespace collapse to fold into a single space. Deleting the whole control range would
    have turned a tab-separated "Ramesh\\tKumar" into "RameshKumar" — one unsearchable token,
    which is the same defect this function exists to prevent.
    """
    return _WHITESPACE_RUN.sub(" ", _NON_SPACING_CONTROL_CHARS.sub("", value)).strip()


def _validate_full_name(value: str | None) -> str | None:
    """Reject a name that is not a name; normalise one that is.

    ``min_length=1`` passed "   ", "\\t\\n", and a lone control character, each of which created
    a real chart -- consent recorded, documents attachable, suggestions loggable -- that no
    clinician can identify in a patient list or find by search. There is no recovering from
    that afterwards either: the name is the only human-readable handle on the record.
    """
    if value is None:
        return value
    cleaned = _clean_identifier(value)
    if not cleaned:
        raise ValueError(
            "full_name cannot be blank — a chart with no readable name cannot be identified "
            "in the patient list or found by search"
        )
    return cleaned


def _validate_phone(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _clean_identifier(value)
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
    return _CONTROL_CHARS_KEEPING_LINES.sub("", value.replace("\r\n", "\n"))


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

    _check_dob = field_validator("date_of_birth")(_validate_dob)
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

    _check_dob = field_validator("date_of_birth")(_validate_dob)
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
