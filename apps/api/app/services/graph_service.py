"""Patient graph assembly: merge approved extractions into the longitudinal record (P1-06).

Deduplicates against existing entities, marks merged data clinician-confirmed, and computes
derived markers (eGFR via CKD-EPI 2021) when the inputs are available.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clinical import (
    age_from_dob,
    ckd_epi_2021_egfr,
    egfr_reference_abnormal,
    is_creatinine_marker,
)
from app.core.dates import is_plausible_clinical_date, parse_clinical_date
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.document import Document
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.services.drug_resolver import DrugResolver


def _resolvable_names(entities: list[dict]) -> list[str]:
    """Every drug name in a merge payload that will be looked up in the vocabulary.

    Mirrors what ``_merge_medication`` and ``_merge_allergy`` resolve — medications by brand
    (falling back to generic), drug allergies by allergen name — so the prefetch covers
    exactly the lookups the merge is about to make and nothing more.
    """
    names: list[str] = []
    for entity in entities:
        fields = entity.get("fields", {})
        if entity.get("entity_type") == "medication":
            name = fields.get("brand_name_raw") or fields.get("generic_name")
            if isinstance(name, str):
                names.append(name)
        elif (
            entity.get("entity_type") == "allergy"
            and (fields.get("allergen_type") or "drug") == "drug"
        ):
            name = fields.get("allergen_name")
            if isinstance(name, str):
                names.append(name)
    return names


def _as_text(value: object) -> object:
    """Render a non-string scalar as text, leaving strings and ``None`` alone.

    The merge treats extracted fields as text throughout — ``name.lower()``, ``.strip()`` in
    the drug resolver, assignment into ``String(500)`` columns — but nothing upstream had
    guaranteed that. A field that arrived as a number (a clinician correcting a dose to ``500``
    rather than ``"500"``, or a vision model emitting an unquoted value) reached
    ``resolve(500)`` and died on ``AttributeError: 'int' object has no attribute 'strip'``, an
    unhandled 500 that rolled the whole approval back.

    ``_resolvable_names`` already guarded its half of this with an ``isinstance`` check, which
    is why the prefetch survived what the merge did not. Normalising once at the entry to the
    merge covers every consumer instead of one, and costs the downstream numeric paths nothing:
    they are ``Decimal(str(value))`` and ``dtparser.parse(str(value))`` already.
    """
    if value is None or isinstance(value, str):
        return value
    return str(value)


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class GraphService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.resolver = DrugResolver(db)

    async def merge_entities(
        self,
        *,
        patient: Patient,
        document: Document | None,
        entities: list[dict],
    ) -> dict[str, int]:
        """Merge a list of {entity_type, fields, region} dicts. Returns per-type counts."""
        counts = {"medications": 0, "lab_results": 0, "conditions": 0, "allergies": 0}
        source_doc_id = document.id if document else None
        new_lab_results: list[LabResult] = []

        # Load each dedup key set once instead of re-querying per entity (a 12-line
        # prescription used to issue 12 full scans of the patient's medications). Keys added
        # during this merge are folded back in, so a document that lists the same drug or
        # condition twice no longer creates two rows — autoflush is off, so the earlier
        # db.add() would not have been visible to a follow-up SELECT.
        seen = await self._existing_keys(patient)
        # Every medication and drug-allergy line resolves a name against the vocabulary. Doing
        # that lazily costs one exact-match query per line, so a 30-line prescription pays 30
        # round-trips; one prefetch collapses them into a single query.
        await self.resolver.prefetch(_resolvable_names(entities))

        for entity in entities:
            etype = entity.get("entity_type")
            # Every merge below treats these as text; see _as_text for what used to arrive.
            fields = {k: _as_text(v) for k, v in entity.get("fields", {}).items()}
            region = entity.get("region")
            confidence = entity.get("confidence", {})

            if etype == "medication":
                if await self._merge_medication(
                    patient, source_doc_id, fields, region, confidence, seen["medications"]
                ):
                    counts["medications"] += 1
            elif etype == "lab_result":
                lab = await self._merge_lab(patient, source_doc_id, fields, region, confidence)
                if lab is not None:
                    counts["lab_results"] += 1
                    new_lab_results.append(lab)
            elif etype == "condition":
                if self._merge_condition(
                    patient, source_doc_id, fields, region, confidence, seen["conditions"]
                ):
                    counts["conditions"] += 1
            elif etype == "allergy":
                if await self._merge_allergy(
                    patient, source_doc_id, fields, region, confidence, seen["allergies"]
                ):
                    counts["allergies"] += 1

        await self.db.flush()
        await self._compute_derived_markers(patient, new_lab_results)
        await self.db.flush()
        return counts

    async def _existing_keys(self, patient: Patient) -> dict[str, set]:
        """Dedup keys already in the record: current meds, conditions, allergies."""
        meds = await self.db.execute(
            select(MedicationEvent.generic_name, MedicationEvent.dose).where(
                MedicationEvent.patient_id == patient.id,
                MedicationEvent.is_deleted.is_(False),
                MedicationEvent.is_current.is_(True),
            )
        )
        conditions = await self.db.execute(
            select(Condition.condition_name).where(
                Condition.patient_id == patient.id,
                Condition.is_deleted.is_(False),
            )
        )
        allergies = await self.db.execute(
            select(Allergy.allergen_name).where(
                Allergy.patient_id == patient.id,
                Allergy.is_deleted.is_(False),
            )
        )
        return {
            "medications": {((generic or "").lower(), dose or "") for generic, dose in meds.all()},
            "conditions": {name.lower() for (name,) in conditions.all() if name},
            "allergies": {name.lower() for (name,) in allergies.all() if name},
        }

    async def _merge_medication(
        self,
        patient: Patient,
        source_doc_id: uuid.UUID | None,
        fields: dict[str, Any],
        region: dict[str, Any] | None,
        confidence: dict[str, Any],
        seen: set,
    ) -> bool:
        brand = fields.get("brand_name_raw") or fields.get("generic_name")
        resolved = await self.resolver.resolve(brand)
        generic = resolved.generic_name if resolved else fields.get("generic_name")
        vocab_id = resolved.vocabulary_id if resolved else None

        # Dedup: same generic + dose among the patient's current medications.
        key = ((generic or "").lower(), fields.get("dose") or "")
        if generic and key in seen:
            return False
        seen.add(key)

        med = MedicationEvent(
            patient_id=patient.id,
            source_document_id=source_doc_id,
            drug_vocabulary_id=vocab_id,
            brand_name_raw=fields.get("brand_name_raw"),
            generic_name=generic,
            dose=fields.get("dose"),
            dose_unit=fields.get("dose_unit"),
            frequency=fields.get("frequency"),
            route=fields.get("route"),
            event_type=fields.get("event_type") or "continue",
            event_date=_parse_date(fields.get("event_date")),
            is_current=True,
            extraction_region=region,
            extraction_confidence=confidence,
            clinician_confirmed=True,
            clinician_confirmed_at=datetime.now(UTC),
        )
        self.db.add(med)
        return True

    async def _merge_lab(
        self,
        patient: Patient,
        source_doc_id: uuid.UUID | None,
        fields: dict[str, Any],
        region: dict[str, Any] | None,
        confidence: dict[str, Any],
    ) -> LabResult | None:
        marker = fields.get("marker_name")
        if not marker:
            return None
        value_numeric = _to_decimal(fields.get("value_numeric"))
        low = _to_decimal(fields.get("reference_range_low"))
        high = _to_decimal(fields.get("reference_range_high"))

        is_abnormal: bool | None = None
        direction: str | None = None
        if value_numeric is not None and (low is not None or high is not None):
            if high is not None and value_numeric > high:
                is_abnormal, direction = True, "high"
            elif low is not None and value_numeric < low:
                is_abnormal, direction = True, "low"
            else:
                is_abnormal = False

        sample_date = _parse_datetime(fields.get("sample_date"))
        lab = LabResult(
            patient_id=patient.id,
            source_document_id=source_doc_id,
            marker_name=marker,
            value_numeric=value_numeric,
            value_text=str(fields.get("value_numeric"))
            if fields.get("value_numeric") is not None
            else fields.get("value_text"),
            unit=fields.get("unit"),
            reference_range_low=low,
            reference_range_high=high,
            is_abnormal=is_abnormal,
            abnormality_direction=direction,
            sample_date=sample_date,
            extraction_region=region,
            extraction_confidence=confidence,
            clinician_confirmed=True,
            clinician_confirmed_at=datetime.now(UTC),
        )
        self.db.add(lab)
        return lab

    def _merge_condition(
        self,
        patient: Patient,
        source_doc_id: uuid.UUID | None,
        fields: dict[str, Any],
        region: dict[str, Any] | None,
        confidence: dict[str, Any],
        seen: set,
    ) -> bool:
        name = fields.get("condition_name")
        if not name:
            return False
        if name.lower() in seen:
            return False
        seen.add(name.lower())
        cond = Condition(
            patient_id=patient.id,
            source_document_id=source_doc_id,
            condition_name=name,
            icd10_code=fields.get("icd10_code"),
            status=fields.get("status") or "active",
            severity=fields.get("severity"),
            extraction_region=region,
            extraction_confidence=confidence,
            clinician_confirmed=True,
            clinician_confirmed_at=datetime.now(UTC),
        )
        self.db.add(cond)
        return True

    async def _merge_allergy(
        self,
        patient: Patient,
        source_doc_id: uuid.UUID | None,
        fields: dict[str, Any],
        region: dict[str, Any] | None,
        confidence: dict[str, Any],
        seen: set,
    ) -> bool:
        name = fields.get("allergen_name")
        if not name:
            return False
        if name.lower() in seen:
            return False
        seen.add(name.lower())
        allergen_type = fields.get("allergen_type") or "drug"
        vocab_id = None
        if allergen_type == "drug":
            resolved = await self.resolver.resolve(name)
            vocab_id = resolved.vocabulary_id if resolved else None
        allergy = Allergy(
            patient_id=patient.id,
            source_document_id=source_doc_id,
            allergen_name=name,
            allergen_type=allergen_type,
            reaction_description=fields.get("reaction_description"),
            severity=fields.get("severity"),
            status="active",
            drug_vocabulary_id=vocab_id,
            extraction_region=region,
            extraction_confidence=confidence,
            clinician_confirmed=True,
            clinician_confirmed_at=datetime.now(UTC),
        )
        self.db.add(allergy)
        return True

    async def _compute_derived_markers(self, patient: Patient, new_labs: list[LabResult]) -> None:
        if patient.date_of_birth is None or patient.sex not in ("male", "female"):
            return
        for lab in new_labs:
            if not is_creatinine_marker(lab.marker_name) or lab.value_numeric is None:
                continue
            try:
                age = age_from_dob(
                    patient.date_of_birth, (lab.sample_date or datetime.now(UTC)).date()
                )
                result = ckd_epi_2021_egfr(
                    creatinine_mg_dl=float(lab.value_numeric),
                    age_years=age,
                    sex=patient.sex,
                )
            except ValueError:
                continue
            marker = DerivedMarker(
                patient_id=patient.id,
                source_lab_result_id=lab.id,
                marker_name="eGFR",
                value_numeric=Decimal(str(result.value)),
                unit="mL/min/1.73m2",
                formula_name=result.formula_name,
                formula_version=result.formula_version,
                input_values=result.inputs,
                reference_range_low=Decimal("90"),
                is_abnormal=egfr_reference_abnormal(result.value),
                computed_at=datetime.now(UTC),
            )
            self.db.add(marker)


def _parse_date(value: object) -> date | None:
    """A clinical date for a ``Date`` column, or ``None``. See ``app.core.dates``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        # datetime subclasses date, so this used to pass straight through into
        # MedicationEvent.event_date carrying a time-of-day the column cannot hold — and it
        # cannot be range-checked in that shape either (comparing a datetime against a date
        # bound raises TypeError).
        as_date: date | None = value.date()
    elif isinstance(value, date):
        as_date = value
    else:
        parsed = parse_clinical_date(str(value))
        as_date = parsed.date() if parsed else None
    if as_date is None or not is_plausible_clinical_date(as_date):
        return None
    return as_date


def _parse_datetime(value: object) -> datetime | None:
    """A clinical timestamp for a ``DateTime`` column, or ``None``. See ``app.core.dates``."""
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else parse_clinical_date(str(value))
    if parsed is None or not is_plausible_clinical_date(parsed.date()):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
