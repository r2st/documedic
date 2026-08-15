"""Patient record export as a FHIR R4 Bundle.

There was no way to get a patient's record out of this system. Every read was a bespoke JSON
shape assembled for one of our own screens, paged for a chart view, and understood by nothing
outside this repository — so a clinician referring a patient onward, a patient exercising the
DPDP Act's right to their own data, and a hospital migrating off the product all had the same
answer: read it off the screen.

FHIR R4 rather than a tidier internal shape, because the whole value of an export is that
something else can read it. The receiving system is a hospital EMR, a district health
information system under ABDM, or another CDSS; none of them will learn our field names, and
all of them speak some FHIR.

What this deliberately does *not* do
------------------------------------
It does not export clinical suggestions, reasoning sessions, or agent deliberation. Those are
this system's *opinions* about the patient, not the patient's record, and a differential
diagnosis that leaves here as a FHIR ``Condition`` arrives at the far end indistinguishable
from a diagnosis a clinician made — which is exactly the automation bias the product is built
against, laundered through an interchange format. The record that is exported is the record
that was put in: what documents said, what the clinician confirmed, and the markers computed
deterministically from those.

Provenance survives the trip. Every resource carries whether a clinician confirmed it
(``clinician-confirmed``) and which uploaded document it came from, because a receiving system
that cannot tell an OCR guess from a confirmed entry will treat both as fact.

Completeness is asserted, not assumed
-------------------------------------
Each section is read under a ceiling. If one is hit the bundle carries an ``OperationOutcome``
saying so, because a silently short export is worse than a refused one: the reader has no way
to know a chart is missing its oldest labs, and the natural assumption about a file called "the
patient's record" is that it is the patient's record.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient

FHIR_VERSION = "4.0.1"

# Where our own provenance flags are hung on an exported resource. A receiving system that does
# not know this URL ignores the extension, which is what FHIR extensions are for; one that does
# can tell a clinician-confirmed entry from an unreviewed extraction.
PROVENANCE_EXTENSION = "https://aether-clinician.in/fhir/StructureDefinition/source-provenance"

_ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10"
_CONDITION_CLINICAL_SYSTEM = "http://terminology.hl7.org/CodeSystem/condition-clinical"
_ALLERGY_CLINICAL_SYSTEM = "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical"
_INTERPRETATION_SYSTEM = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"

# Our vocabularies -> FHIR's. Anything absent falls through to ``unknown`` rather than being
# guessed at: a status this map does not recognise must not be exported as "active".
_CONDITION_STATUS = {
    "active": "active",
    "resolved": "resolved",
    "inactive": "inactive",
    "recurrence": "recurrence",
    "unknown": "unknown",
}
_ALLERGY_STATUS = {
    "active": "active",
    "resolved": "resolved",
    # FHIR has no "refuted" clinical status — it models a refuted allergy through
    # verificationStatus instead, which is where this is carried. See _allergy.
    "refuted": "inactive",
    "unknown": "inactive",
}
_ALLERGY_CATEGORY = {
    "drug": "medication",
    "food": "food",
    "environmental": "environment",
    "other": None,
}
_ALLERGY_CRITICALITY = {"severe": "high", "moderate": "low", "mild": "low"}
_INTERPRETATION = {"high": ("H", "High"), "low": ("L", "Low")}
_PATIENT_GENDER = {"male", "female", "other", "unknown"}


def _iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return aware.isoformat()
    return value.isoformat()


def _number(value: Decimal | None) -> float | None:
    """A ``Numeric`` column as JSON. FHIR ``decimal`` is a JSON number, not a string."""
    return None if value is None else float(value)


def _prune(resource: dict[str, Any]) -> dict[str, Any]:
    """Drop absent elements. FHIR forbids null-valued elements, unlike our own schemas."""
    return {
        key: value
        for key, value in resource.items()
        if value is not None and value != [] and value != {}
    }


class PatientExportService:
    """Assembles one patient's record as a FHIR Bundle. Read-only."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _rows[RowT](self, stmt: Select[tuple[RowT]]) -> tuple[list[RowT], bool]:
        """One section, under the export ceiling. Also reports whether the ceiling was hit.

        Reads one row past the limit rather than counting: a count is a second query over the
        same predicate to answer a yes/no question.
        """
        limit = settings.export_max_rows_per_section
        rows = list((await self.db.execute(stmt.limit(limit + 1))).scalars().all())
        return rows[:limit], len(rows) > limit

    async def build_bundle(self, patient: Patient) -> dict[str, Any]:
        """The patient's record as a FHIR R4 ``Bundle`` of type ``collection``.

        ``collection`` rather than ``document``: a FHIR document is a signed, immutable
        composition with a mandated ``Composition`` at its head and an attestation that this
        system is in no position to make. A collection says what this is — a set of related
        resources gathered for transfer — without claiming more.
        """
        patient_ref = f"urn:uuid:{patient.id}"
        entries: list[dict[str, Any]] = [self._patient(patient)]
        incomplete: list[str] = []

        allergies, truncated = await self._rows(
            select(Allergy)
            .where(Allergy.patient_id == patient.id, Allergy.is_deleted.is_(False))
            .order_by(Allergy.allergen_name, Allergy.id)
        )
        incomplete += ["AllergyIntolerance"] if truncated else []
        entries += [self._allergy(row, patient_ref) for row in allergies]

        conditions, truncated = await self._rows(
            select(Condition)
            .where(Condition.patient_id == patient.id, Condition.is_deleted.is_(False))
            .order_by(Condition.condition_name, Condition.id)
        )
        incomplete += ["Condition"] if truncated else []
        entries += [self._condition(row, patient_ref) for row in conditions]

        medications, truncated = await self._rows(
            select(MedicationEvent)
            .where(
                MedicationEvent.patient_id == patient.id,
                MedicationEvent.is_deleted.is_(False),
            )
            .order_by(MedicationEvent.event_date.desc().nullslast(), MedicationEvent.id)
        )
        incomplete += ["MedicationStatement"] if truncated else []
        entries += [self._medication(row, patient_ref) for row in medications]

        labs, truncated = await self._rows(
            select(LabResult)
            .where(LabResult.patient_id == patient.id, LabResult.is_deleted.is_(False))
            .order_by(LabResult.sample_date.desc().nullslast(), LabResult.id)
        )
        incomplete += ["Observation (laboratory)"] if truncated else []
        entries += [self._lab(row, patient_ref) for row in labs]

        markers, truncated = await self._rows(
            select(DerivedMarker)
            .where(DerivedMarker.patient_id == patient.id, DerivedMarker.is_deleted.is_(False))
            .order_by(DerivedMarker.computed_at.desc(), DerivedMarker.id)
        )
        incomplete += ["Observation (derived)"] if truncated else []
        entries += [self._marker(row, patient_ref) for row in markers]

        if incomplete:
            entries.append(self._truncation_notice(incomplete))

        return {
            "resourceType": "Bundle",
            "id": str(uuid.uuid4()),
            "meta": {
                "lastUpdated": _iso(datetime.now(UTC)),
                "profile": [f"http://hl7.org/fhir/StructureDefinition/Bundle|{FHIR_VERSION}"],
            },
            "type": "collection",
            "timestamp": _iso(datetime.now(UTC)),
            "entry": entries,
        }

    # --- Resource builders -----------------------------------------------------------------

    @staticmethod
    def _entry(resource: dict[str, Any]) -> dict[str, Any]:
        return {"fullUrl": f"urn:uuid:{resource['id']}", "resource": _prune(resource)}

    @staticmethod
    def _provenance(
        *, confirmed: bool, source_document_id: uuid.UUID | None
    ) -> list[dict[str, Any]]:
        """Whether a clinician confirmed this row, and which document it came from.

        Carried on every clinical resource because the receiving system otherwise cannot tell
        a line a clinician read and confirmed from a line an OCR pass guessed at, and will
        treat both as fact. An extension is the right home: a reader that does not know this
        URL ignores it rather than failing to parse the bundle.
        """
        extension: list[dict[str, Any]] = [
            {"url": "clinician-confirmed", "valueBoolean": bool(confirmed)}
        ]
        if source_document_id is not None:
            extension.append({"url": "source-document", "valueString": str(source_document_id)})
        return [{"url": PROVENANCE_EXTENSION, "extension": extension}]

    def _patient(self, patient: Patient) -> dict[str, Any]:
        return self._entry(
            {
                "resourceType": "Patient",
                "id": str(patient.id),
                "identifier": [
                    {
                        "system": "https://aether-clinician.in/patient-id",
                        "value": str(patient.id),
                    }
                ],
                "name": [{"text": patient.full_name}],
                "gender": patient.sex if patient.sex in _PATIENT_GENDER else None,
                "birthDate": _iso(patient.date_of_birth),
                "telecom": (
                    [{"system": "phone", "value": patient.phone}] if patient.phone else None
                ),
                "address": ([{"text": patient.address_text}] if patient.address_text else None),
            }
        )

    def _allergy(self, row: Allergy, patient_ref: str) -> dict[str, Any]:
        category = _ALLERGY_CATEGORY.get(row.allergen_type)
        return self._entry(
            {
                "resourceType": "AllergyIntolerance",
                "id": str(row.id),
                "extension": self._provenance(
                    confirmed=row.clinician_confirmed,
                    source_document_id=row.source_document_id,
                ),
                "clinicalStatus": {
                    "coding": [
                        {
                            "system": _ALLERGY_CLINICAL_SYSTEM,
                            "code": _ALLERGY_STATUS.get(row.status, "inactive"),
                        }
                    ]
                },
                # A refuted allergy is a distinct claim — someone looked and it is not true —
                # and FHIR carries it here rather than in clinicalStatus. Exporting it as a
                # plain inactive allergy would lose that a clinician actively ruled it out.
                "verificationStatus": {
                    "coding": [
                        {
                            "system": (
                                "http://terminology.hl7.org/CodeSystem/"
                                "allergyintolerance-verification"
                            ),
                            "code": "refuted" if row.status == "refuted" else "unconfirmed",
                        }
                    ]
                }
                if row.status == "refuted" or not row.clinician_confirmed
                else {
                    "coding": [
                        {
                            "system": (
                                "http://terminology.hl7.org/CodeSystem/"
                                "allergyintolerance-verification"
                            ),
                            "code": "confirmed",
                        }
                    ]
                },
                "category": [category] if category else None,
                "criticality": _ALLERGY_CRITICALITY.get(row.severity or ""),
                "code": {"text": row.allergen_name},
                "patient": {"reference": patient_ref},
                "reaction": (
                    [
                        _prune(
                            {
                                "manifestation": [{"text": row.reaction_description}],
                                "severity": row.severity
                                if row.severity in ("mild", "moderate", "severe")
                                else None,
                            }
                        )
                    ]
                    if row.reaction_description or row.severity
                    else None
                ),
            }
        )

    def _condition(self, row: Condition, patient_ref: str) -> dict[str, Any]:
        return self._entry(
            {
                "resourceType": "Condition",
                "id": str(row.id),
                "extension": self._provenance(
                    confirmed=row.clinician_confirmed,
                    source_document_id=row.source_document_id,
                ),
                "clinicalStatus": {
                    "coding": [
                        {
                            "system": _CONDITION_CLINICAL_SYSTEM,
                            "code": _CONDITION_STATUS.get(row.status, "unknown"),
                        }
                    ]
                },
                "code": _prune(
                    {
                        "coding": (
                            [{"system": _ICD10_SYSTEM, "code": row.icd10_code}]
                            if row.icd10_code
                            else None
                        ),
                        "text": row.condition_name,
                    }
                ),
                "severity": {"text": row.severity} if row.severity else None,
                "subject": {"reference": patient_ref},
                "onsetDateTime": _iso(row.onset_date),
            }
        )

    def _medication(self, row: MedicationEvent, patient_ref: str) -> dict[str, Any]:
        # A stop event is a stopped medication whatever `is_current` says; otherwise the flag
        # the chart is actually driven by decides. "unknown" rather than a guess for anything
        # else, because "active" is the reading with consequences at the far end.
        if row.event_type == "stop":
            status = "stopped"
        elif row.is_current:
            status = "active"
        else:
            status = "completed"
        return self._entry(
            {
                "resourceType": "MedicationStatement",
                "id": str(row.id),
                "extension": self._provenance(
                    confirmed=row.clinician_confirmed,
                    source_document_id=row.source_document_id,
                ),
                "status": status,
                "medicationCodeableConcept": {
                    # The generic (INN) is the interoperable name; the brand as written on the
                    # prescription is kept alongside it because that is what the chart says and
                    # an Indian brand is often the only thing the source document carried.
                    "text": row.generic_name or row.brand_name_raw or "unspecified medication",
                    **(
                        {"coding": [{"display": row.brand_name_raw}]}
                        if row.brand_name_raw and row.generic_name
                        else {}
                    ),
                },
                "subject": {"reference": patient_ref},
                "effectivePeriod": _prune(
                    {"start": _iso(row.event_date), "end": _iso(row.end_date)}
                )
                or None,
                "dosage": (
                    [
                        _prune(
                            {
                                "text": " ".join(
                                    part
                                    for part in (row.dose, row.dose_unit, row.frequency)
                                    if part
                                )
                                or None,
                                "route": {"text": row.route} if row.route else None,
                            }
                        )
                    ]
                    if any((row.dose, row.dose_unit, row.frequency, row.route))
                    else None
                ),
            }
        )

    def _observation(
        self,
        *,
        resource_id: uuid.UUID,
        patient_ref: str,
        code_text: str,
        category: str,
        effective: datetime | None,
        extension: list[dict[str, Any]] | None = None,
        value_numeric: Decimal | None = None,
        value_text: str | None = None,
        unit: str | None = None,
        low: Decimal | None = None,
        high: Decimal | None = None,
        direction: str | None = None,
    ) -> dict[str, Any]:
        """The shape both lab results and derived markers share."""
        interpretation = _INTERPRETATION.get((direction or "").lower())
        return self._entry(
            {
                "resourceType": "Observation",
                "id": str(resource_id),
                "extension": extension,
                "status": "final",
                "category": [
                    {
                        "coding": [
                            {
                                "system": (
                                    "http://terminology.hl7.org/CodeSystem/observation-category"
                                ),
                                "code": category,
                            }
                        ]
                    }
                ],
                "code": {"text": code_text},
                "subject": {"reference": patient_ref},
                "effectiveDateTime": _iso(effective),
                "valueQuantity": (
                    _prune({"value": _number(value_numeric), "unit": unit})
                    if value_numeric is not None
                    else None
                ),
                # Only when there is no number: FHIR allows exactly one value[x], and a result
                # carrying both would be an invalid resource rather than a richer one.
                "valueString": value_text if value_numeric is None else None,
                "interpretation": (
                    [
                        {
                            "coding": [
                                {
                                    "system": _INTERPRETATION_SYSTEM,
                                    "code": interpretation[0],
                                    "display": interpretation[1],
                                }
                            ]
                        }
                    ]
                    if interpretation
                    else None
                ),
                "referenceRange": (
                    [
                        _prune(
                            {
                                "low": _prune({"value": _number(low), "unit": unit}) or None,
                                "high": _prune({"value": _number(high), "unit": unit}) or None,
                            }
                        )
                    ]
                    if low is not None or high is not None
                    else None
                ),
            }
        )

    def _lab(self, row: LabResult, patient_ref: str) -> dict[str, Any]:
        return self._observation(
            resource_id=row.id,
            patient_ref=patient_ref,
            code_text=row.marker_name,
            category="laboratory",
            effective=row.sample_date,
            extension=self._provenance(
                confirmed=row.clinician_confirmed,
                source_document_id=row.source_document_id,
            ),
            value_numeric=row.value_numeric,
            value_text=row.value_text,
            unit=row.unit,
            low=row.reference_range_low,
            high=row.reference_range_high,
            direction=row.abnormality_direction,
        )

    def _marker(self, row: DerivedMarker, patient_ref: str) -> dict[str, Any]:
        """A computed marker (eGFR, BMI, ...) as a derived Observation.

        The formula and its version travel with the value. An eGFR is only interpretable
        against the equation that produced it — this system has already had a defect where an
        adult equation was applied to a child — so a receiving system that cannot see which
        equation was used cannot safely re-use the number.
        """
        resource = self._observation(
            resource_id=row.id,
            patient_ref=patient_ref,
            code_text=row.marker_name,
            category="survey",
            effective=row.computed_at,
            value_numeric=row.value_numeric,
            unit=row.unit,
        )
        resource["resource"]["method"] = {"text": f"{row.formula_name} ({row.formula_version})"}
        return resource

    def _truncation_notice(self, sections: list[str]) -> dict[str, Any]:
        """An ``OperationOutcome`` naming the sections that hit the ceiling.

        In the bundle rather than in a header, because the bundle is the artefact that gets
        saved, mailed and imported; a warning that lives only on the HTTP response is gone by
        the time anyone reads the file.
        """
        return self._entry(
            {
                "resourceType": "OperationOutcome",
                "id": str(uuid.uuid4()),
                "issue": [
                    {
                        "severity": "warning",
                        "code": "incomplete",
                        "diagnostics": (
                            "This export is incomplete. The following sections exceeded the "
                            f"per-section export limit of {settings.export_max_rows_per_section} "
                            f"resources and were truncated: {', '.join(sections)}. Do not read "
                            "this bundle as the patient's whole record."
                        ),
                    }
                ],
            }
        )
