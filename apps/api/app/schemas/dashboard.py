"""Clinical dashboard response shapes.

Every count here is scoped to the calling account. Nothing in this module is patient-identifying
— they are aggregates over a panel — but the *shape* of a panel is still information about a
clinic, which is why the scoping is asserted in tests rather than assumed from the SQL.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PatientCounts(BaseModel):
    """Panel size by the states a chart can actually be in.

    There is no status column on `patients`; these are the three properties that decide what a
    clinician can do with a chart — whether it has been withdrawn, whether consent to process it
    is recorded, and whether it carries the demographics the safety engine needs.
    """

    # Every chart ever created on this account, withdrawn included, so `active` + `withdrawn`
    # adds up to something a reader can check.
    total: int = 0
    active: int = 0
    withdrawn: int = 0
    consent_recorded: int = 0
    consent_missing: int = 0
    # A chart with no date of birth runs no age-based check; one with no weight runs no
    # paediatric dose check. Both are gaps in what the safety engine can say, not just in the
    # demographics.
    date_of_birth_missing: int = 0
    weight_missing: int = 0


class EncounterCounts(BaseModel):
    """Visit volume, by what the visit was and by how far through its lifecycle it is."""

    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    # A large `draft` count is a backlog of visit notes nobody has signed — the operationally
    # interesting number, and one a total hides.
    by_status: dict[str, int] = Field(default_factory=dict)


class PrescribedDrug(BaseModel):
    drug: str
    # Distinct patients, not rows: a titration that produced six `change` events for one patient
    # is one patient on that drug.
    patient_count: int = 0
    event_count: int = 0


class MedicationCounts(BaseModel):
    limit: int = 0
    most_prescribed: list[PrescribedDrug] = Field(default_factory=list)


class SafetyCheckCount(BaseModel):
    check_type: str
    count: int = 0
    hard_blocks: int = 0


class SafetyFlagCounts(BaseModel):
    """How often each deterministic check has fired on this account.

    Cumulative over the account's history — `drug_safety_checks` is append-only — rather than a
    snapshot of what is currently flagged. The question this answers is which alert clinicians
    are seeing most often, which is the one that decides whether an alert is still being read.
    """

    total: int = 0
    hard_blocks: int = 0
    by_check_type: list[SafetyCheckCount] = Field(default_factory=list)
    by_severity: dict[str, int] = Field(default_factory=dict)


class DashboardOverview(BaseModel):
    patients: PatientCounts
    encounters: EncounterCounts
    medications: MedicationCounts
    safety_flags: SafetyFlagCounts
