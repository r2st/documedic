"""Prescription-history timeline response shapes."""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, Field


class TimelineEntryOut(BaseModel):
    """One medication event, placed in its own drug's history."""

    id: uuid.UUID
    event_type: str
    event_date: date | None = None
    end_date: date | None = None
    dose: str | None = None
    dose_unit: str | None = None
    frequency: str | None = None
    route: str | None = None
    # The three dose columns as the one string a clinician compares — "500 mg BD".
    dose_text: str | None = None
    # What the dose was before this entry, when it changed. Null both for the first entry and
    # when nothing changed; `dose_changed` is what tells the two apart.
    previous_dose_text: str | None = None
    dose_changed: bool = False
    duration_text: str | None = None
    prescriber_name: str | None = None
    is_current: bool = False
    clinician_confirmed: bool = False
    source_document_id: uuid.UUID | None = None
    # The visit this event was recorded at, where the record links one.
    encounter_id: uuid.UUID | None = None
    encounter_date: date | None = None
    encounter_type: str | None = None


class TimelineDrugOut(BaseModel):
    """Every event this record holds about one drug, and what they add up to."""

    drug_vocabulary_id: uuid.UUID | None = None
    reference_id: str | None = None
    generic_name: str | None = None
    brand_name_raw: str | None = None
    display_name: str
    # True when the vocabulary could not identify this drug. Every interaction, contraindication
    # and allergy rule is keyed on a reference id, so none of them ran for this therapy — a
    # timeline that looked the same either way would hide that.
    unresolved: bool = False
    started_on: date | None = None
    stopped_on: date | None = None
    is_current: bool = False
    dose_change_count: int = 0
    event_count: int = 0
    # Oldest first: a titration is a sequence, and each entry's `previous_dose_text` refers to
    # the one before it in this list.
    entries: list[TimelineEntryOut] = Field(default_factory=list)


class PrescriptionTimelineResponse(BaseModel):
    patient_id: uuid.UUID
    # Current therapy first, then most recently touched, then by name.
    medications: list[TimelineDrugOut] = Field(default_factory=list)
    total_drugs: int = 0
    total_events: int = 0
