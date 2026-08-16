"""Medication-reconciliation request/response schemas."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.text_sanitize import clean_identifier
from app.schemas.safety import (
    MAX_DOSE_CHARS,
    MAX_DOSE_UNIT_CHARS,
    MAX_DRUG_NAME_CHARS,
    MAX_FREQUENCY_CHARS,
    SafetyFlagResponse,
)
from app.services.med_reconciliation_service import MAX_PROPOSED_MEDICATIONS

# The transition being reconciled at. A closed vocabulary rather than free text, for two
# reasons: it goes onto the audit trail, where free text a clinician typed does not belong
# (tests/test_audit_payload_free_text.py), and its whole purpose is to be aggregated later —
# "how many discharge reconciliations did this practice do" is not a question answerable over
# prose. It changes no logic; see ``MedReconciliationService.reconcile``.
ReconciliationContext = Literal[
    "admission",
    "discharge",
    "transfer",
    "outpatient_review",
]


class ProposedMedicationRequest(BaseModel):
    """One line of the medication list being reconciled *to*.

    ``name`` accepts what is written on the source document — an Indian brand name is expected
    and is the ordinary case, since the list usually arrives as somebody else's discharge
    summary. It resolves through the DrugVocabulary exactly as the single-drug check's does.

    A name that resolves to nothing does **not** fail the request. That is a deliberate
    difference from ``POST ../check``, where an unresolved name is a 422 because the entire
    request was about that one drug and answering it would mean reporting "no problems found"
    for a drug nothing checked. Here the request is about a *list*, and refusing the whole list
    because one line of eleven is an unseeded brand would leave the clinician with no
    reconciliation at all — the strictly worse outcome, since the other ten lines contain the
    omissions and interactions this endpoint exists to surface. The unresolved line is instead
    carried into the response under its own disposition and counted in `unresolved_proposed`,
    so what was not compared is stated rather than implied.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=MAX_DRUG_NAME_CHARS,
        description="Brand or generic name, as written on the source list",
    )
    dose: str | None = Field(default=None, max_length=MAX_DOSE_CHARS)
    dose_unit: str | None = Field(default=None, max_length=MAX_DOSE_UNIT_CHARS)
    frequency: str | None = Field(default=None, max_length=MAX_FREQUENCY_CHARS)

    @field_validator("name", "dose", "dose_unit", "frequency")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        # Same cleaning as SafetyCheckRequest, for the same two reasons: the resolver matches on
        # the string, so a doubled space is a silent no-match; and an unresolved name is quoted
        # back into a clinician-facing line.
        if value is None:
            return None
        return clean_identifier(value) or None

    @field_validator("name")
    @classmethod
    def _name_is_not_blank(cls, value: str | None) -> str:
        if not value:
            raise ValueError("name must contain at least one non-whitespace character")
        return value


class MedicationReconciliationRequest(BaseModel):
    context: ReconciliationContext = Field(
        ..., description="The transition of care this reconciliation is being performed at"
    )
    medications: list[ProposedMedicationRequest] = Field(
        ...,
        min_length=1,
        max_length=MAX_PROPOSED_MEDICATIONS,
        description="The medication list being reconciled to",
    )


class ReconciliationLineResponse(BaseModel):
    disposition: str = Field(
        ...,
        description=(
            "continue | dose_change | start | stop | unresolved_proposed | unresolved_charted. "
            "A statement about the two lists, never an instruction."
        ),
    )
    label: str
    summary: str
    proposed_name: str | None = None
    charted_name: str | None = None
    reference_id: str | None = None
    proposed_dose: str | None = None
    charted_dose: str | None = None
    details: dict = Field(default_factory=dict)


class ReconciliationFlagResponse(BaseModel):
    finding: str = Field(
        ...,
        description=(
            "intra_list_interaction | intra_list_duplicate | intra_list_class_overlap | "
            "high_risk_omission"
        ),
    )
    severity: str
    summary: str
    details: dict = Field(default_factory=dict)
    drug_interaction_id: uuid.UUID | None = None


class MedicationReconciliationResponse(BaseModel):
    patient_id: uuid.UUID
    context: str
    lines: list[ReconciliationLineResponse]
    # Findings about the proposed list itself — the pairs and omissions no per-drug check can
    # reach. Kept apart from `safety_flags` because the two answer different questions and a
    # clinician acts on them differently: a list flag is resolved by editing the list, a safety
    # flag by a decision about this patient.
    list_flags: list[ReconciliationFlagResponse]
    # The per-patient drug-safety findings for every proposed drug, plus the chart-level notes
    # once. Same shape as `POST ../check` returns, and the hard blocks carry the same `id`, so
    # `POST ../override` works against them unchanged.
    safety_flags: list[SafetyFlagResponse]
    is_blocked: bool = Field(
        ...,
        description=(
            "True if any proposed medication raised a hard block (allergy/contraindication)"
        ),
    )
    # How much of the comparison actually happened. Leading with this rather than burying it in
    # the line list, because a reconciliation report is read as a statement that the two lists
    # have been compared, and a name the vocabulary could not identify was compared against
    # nothing.
    proposed_count: int
    charted_count: int
    reconciled_count: int = Field(
        ..., description="Lines where both lists were actually compared against each other"
    )
    unresolved_proposed: list[str] = Field(
        default_factory=list,
        description="Proposed names that matched no known drug and so were not compared",
    )
    unresolved_charted: list[str] = Field(
        default_factory=list,
        description="Charted current medications that matched no known drug",
    )
    offline_capable: bool = True
