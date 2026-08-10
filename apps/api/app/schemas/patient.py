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

# Age-based clinical computations (eGFR, renal dosing thresholds) depend on a plausible DOB.
_MAX_PLAUSIBLE_AGE_YEARS = 130


def _validate_dob(value: date | None) -> date | None:
    if value is None:
        return value
    today = date.today()
    if value > today:
        raise ValueError("date_of_birth cannot be in the future")
    if today.year - value.year > _MAX_PLAUSIBLE_AGE_YEARS:
        raise ValueError(f"date_of_birth implies an age over {_MAX_PLAUSIBLE_AGE_YEARS} years")
    return value


def _validate_phone(value: str | None) -> str | None:
    if value is None or not value.strip():
        return value
    if not _PHONE_RE.match(value.strip()):
        raise ValueError("phone must contain only digits, spaces, and + - ( ) separators")
    return value


class PatientCreate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=500)
    date_of_birth: date | None = None
    sex: Sex | None = None
    phone: str | None = Field(default=None, max_length=20)
    address_text: str | None = None
    notes: str | None = None
    consent_given: bool = Field(
        ..., description="Must be true before clinical data is stored (DPDP Act)."
    )

    _check_dob = field_validator("date_of_birth")(_validate_dob)
    _check_phone = field_validator("phone")(_validate_phone)


class PatientUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=500)
    date_of_birth: date | None = None
    sex: Sex | None = None
    phone: str | None = Field(default=None, max_length=20)
    address_text: str | None = None
    notes: str | None = None
    consent_given: bool | None = None

    _check_dob = field_validator("date_of_birth")(_validate_dob)
    _check_phone = field_validator("phone")(_validate_phone)


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
