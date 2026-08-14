"""Patient graph assembly: merge approved extractions into the longitudinal record (P1-06).

Deduplicates against existing entities, marks merged data clinician-confirmed, and computes
derived markers (eGFR via CKD-EPI 2021) when the inputs are available.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from sqlalchemy import Numeric, select
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
from app.models.lab_result import LabResult, lab_observation_key
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
            # Same normalisation _merge_allergy applies, so an "Drug"/"drug " spelling is
            # prefetched rather than falling through to a lazy per-line lookup.
            and _enum(Allergy, "allergen_type", fields.get("allergen_type"), "drug") == "drug"
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


# What Numeric(18, 6) can physically hold, read off the column so it tracks the schema rather
# than a number copied into this file. Scale 6 fixes the quantum; the remaining 12 integer digits
# fix the ceiling.
_VALUE_COLUMN = cast(Numeric, LabResult.__table__.c.value_numeric.type)
_DECIMAL_QUANTUM = Decimal(1).scaleb(-int(_VALUE_COLUMN.scale or 0))
_DECIMAL_CEILING = Decimal(10) ** (
    int(_VALUE_COLUMN.precision or 0) - int(_VALUE_COLUMN.scale or 0)
)


def _to_decimal(value: object) -> Decimal | None:
    """An extracted value as a number the column can hold, or ``None`` if it cannot hold it.

    ``None`` covers three things beyond "not a number at all", and all three arrive from OCR and
    from vision models rather than from anything a person typed:

    * a magnitude past the column's range. ``Numeric(18, 6)`` keeps 12 integer digits, and a
      smudged decimal point in a scanned report turns one lab value into twenty digits. SQLite
      stores it happily; PostgreSQL raises ``numeric field overflow`` at flush, which 500s the
      approval and takes every *other* entity on that document down with it.
    * a non-finite value. ``Decimal("inf")`` and ``Decimal("nan")`` parse without complaint, and
      a NaN compares false against every reference bound — so a value nobody can interpret would
      be recorded as a lab result that is *not* abnormal.
    * more precision than the column keeps. Quantizing here rather than letting the database
      round means the row and its ``dedup_key`` describe the same number. A *nonzero* value that
      quantizes all the way to zero is dropped instead of stored, because scale 6 cannot tell
      ``1e-400`` from ``0`` and the two are not the same reading: a stored ``0.000000`` is a
      precise claim the source never made, and for creatinine it is the one value that makes
      eGFR undefined.

    Dropping the numeric is not dropping the reading: ``_merge_lab`` keeps the raw text in
    ``value_text``, which makes the row qualitative — and ``LabSafetyService`` already skips
    qualitative rows rather than coercing them, which is the correct handling for a value that
    could not be read. Refusing the whole approval instead would discard the entities that
    extracted perfectly well alongside it.
    """
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    try:
        quantized = parsed.quantize(_DECIMAL_QUANTUM)
    except InvalidOperation:
        # More digits than the decimal context carries — necessarily past the ceiling below.
        return None
    if abs(quantized) >= _DECIMAL_CEILING:
        return None
    if not quantized and parsed:
        return None  # underflowed to zero; see the third bullet above
    return quantized


# Sentinel so `_enum` can tell "caller omitted `invalid`" from "caller passed None", which is a
# meaningful value for the nullable severity columns.
_UNSET: Any = object()


def _allowed_enum_values(model: type[Any]) -> dict[str, frozenset[str]]:
    """The value sets the model's ``CHECK ... IN (...)`` constraints permit, per column.

    Read out of the constraints rather than restated as a constant here, so the merge and the
    schema cannot drift apart: adding a severity level to the model automatically admits it.
    """
    allowed: dict[str, set[str]] = {}
    for constraint in model.__table__.constraints:
        sqltext = str(getattr(constraint, "sqltext", ""))
        match = re.search(r"(\w+)\s+IN\s*\(([^)]*)\)", sqltext, re.IGNORECASE)
        if match:
            allowed.setdefault(match.group(1), set()).update(
                re.findall(r"'([^']*)'", match.group(2))
            )
    return {column: frozenset(values) for column, values in allowed.items()}


_ENUM_VALUES: dict[str, dict[str, frozenset[str]]] = {
    model.__name__: _allowed_enum_values(model)
    for model in (Allergy, Condition, MedicationEvent, LabResult)
}


def _enum(
    model: type[Any],
    column: str,
    value: object,
    missing: str | None,
    invalid: str | None = _UNSET,
) -> str | None:
    """An extracted value normalised into what the column's CHECK constraint permits.

    ``severity``, ``status``, ``event_type`` and ``allergen_type`` all arrive from extraction and
    all sit behind a ``CHECK ... IN (...)``, but nothing between the two validated them. A vision
    model that answers "moderate-severe", or a clinician correction — ``FieldCorrection.value``
    accepts any 500-character string for any field name — put an unlisted value straight into the
    ``INSERT``, and the constraint rejected it at ``flush``. Unlike the length and range overflows
    above, this one fails on SQLite too; it simply had no test exercising it.

    The cost of that is the same and it is the reason this is coerced rather than raised on: the
    error surfaces at flush, so it does not fail the one bad field, it 500s the approval and rolls
    back every entity that extracted correctly on the same document.

    Falling back is a clinical choice, not just a technical one, so callers pass it explicitly:
    ``None`` for a nullable ``severity`` (absent is honest, invented is not), and ``"continue"``
    for a medication event — the reading under which the patient is still taking the drug and the
    safety checks therefore still run on it.

    ``missing`` and ``invalid`` are separated because a field the document never stated and a
    field whose value could not be read are different claims, and for ``Condition.status`` they
    differ: an absent status keeps the column's long-standing ``"active"`` default, while an
    unreadable one records ``"unknown"`` rather than asserting an active diagnosis nobody made.
    Where the two coincide, ``invalid`` defaults to ``missing``.
    """
    if invalid is _UNSET:
        invalid = missing
    if value is None or value == "":
        return missing
    if isinstance(value, str):
        normalised = value.strip().lower().replace(" ", "_").replace("-", "_")
        if normalised in _ENUM_VALUES[model.__name__].get(column, frozenset()):
            return normalised
    return invalid


def _fitted(model: type[Any], **values: Any) -> dict[str, Any]:
    """Row keyword arguments with every string trimmed to what its own column can hold.

    Same failure as the numeric ceiling above and the same asymmetry behind it: SQLite ignores a
    ``VARCHAR`` length, PostgreSQL raises ``value too long for type character varying(n)``, so an
    over-long extracted string passes every test here and 500s the approval in production. A
    900-character run of OCR noise read as one drug name is enough to reach it, and the
    deterministic parser will produce exactly that from a scan with a bad line break.

    Trimmed rather than refused for the same reason as above — one unreadable line must not cost
    the clinician the rest of the document — and safe to trim because nothing downstream matches
    on a truncated string: the drug vocabulary resolves against the *full* extracted name before
    this runs, so a name too long to be any real drug simply resolves to nothing, exactly as it
    did before it was shortened.

    Limits are read from the mapped columns, so a column that is widened or narrowed does not
    leave a stale constant behind here.
    """
    columns = model.__table__.c
    fitted: dict[str, Any] = {}
    for name, value in values.items():
        limit = getattr(columns[name].type, "length", None) if name in columns else None
        fitted[name] = (
            value[:limit] if isinstance(value, str) and limit and len(value) > limit else value
        )
    return fitted


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
        seen = await self._existing_keys(patient, source_doc_id)
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
                lab = await self._merge_lab(
                    patient, source_doc_id, fields, region, confidence, seen["lab_results"]
                )
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

    async def _existing_keys(
        self, patient: Patient, source_doc_id: uuid.UUID | None
    ) -> dict[str, set]:
        """Dedup keys already in the record: current meds, conditions, allergies, labs.

        The lab set is the one scoped to a *document* rather than to the whole patient, because
        a lab result is not identified by its marker the way a condition is identified by its
        name. The same marker recurs legitimately for years — a creatinine drawn every quarter is
        the entire point of a longitudinal record — so keying on the marker across the chart
        would suppress the follow-ups that matter most. What is unambiguously a duplicate is the
        same observation arriving from the same document twice, which is what re-approving an
        extraction does: ``approve`` re-merges every entity in the stored extraction, and nothing
        stops a clinician approving a second time (a double-clicked button, or a genuine
        re-approval after correcting a drug name). Every other entity type already survived that;
        labs were re-inserted whole, so one document's creatinine and HbA1c appeared twice in the
        chart, read as two separate draws, and derived eGFR was recomputed and stored per copy.

        Scoping the read to the document also keeps its cost flat. Patient-scoped it would load
        a key per lab the patient has ever had — the set that grows fastest here, and the one
        ``ix_lab_results_patient_sample_date`` exists because it reaches thousands of rows.
        """
        labs = await self.db.execute(
            select(LabResult.dedup_key).where(
                LabResult.patient_id == patient.id,
                LabResult.source_document_id == source_doc_id
                if source_doc_id is not None
                else LabResult.source_document_id.is_(None),
                LabResult.is_deleted.is_(False),
            )
        )
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
            # Read back as stored rather than recomputed from the row's columns: the digest is
            # what the unique index constrains, so comparing against anything else could let the
            # in-memory check pass an insert the database then rejects.
            "lab_results": set(labs.scalars().all()),
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
            **_fitted(
                MedicationEvent,
                patient_id=patient.id,
                source_document_id=source_doc_id,
                drug_vocabulary_id=vocab_id,
                brand_name_raw=fields.get("brand_name_raw"),
                generic_name=generic,
                dose=fields.get("dose"),
                dose_unit=fields.get("dose_unit"),
                frequency=fields.get("frequency"),
                route=fields.get("route"),
                event_type=_enum(
                    MedicationEvent, "event_type", fields.get("event_type"), "continue"
                ),
                event_date=_parse_date(fields.get("event_date")),
                is_current=True,
                extraction_region=region,
                extraction_confidence=confidence,
                clinician_confirmed=True,
                clinician_confirmed_at=datetime.now(UTC),
            )
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
        seen: set,
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
        # Same marker, same value, same draw date, same document: the observation is already in
        # the chart. Two *different* values for one marker in one report (a pre- and post-dialysis
        # creatinine) differ in the key and are both kept, as is the same marker arriving from a
        # later report. See _existing_keys for why this is scoped to the document.
        key = lab_observation_key(source_doc_id, marker, value_numeric, sample_date)
        if key in seen:
            return None
        seen.add(key)

        lab = LabResult(
            **_fitted(
                LabResult,
                patient_id=patient.id,
                source_document_id=source_doc_id,
                dedup_key=key,
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
            **_fitted(
                Condition,
                patient_id=patient.id,
                source_document_id=source_doc_id,
                condition_name=name,
                icd10_code=fields.get("icd10_code"),
                status=_enum(Condition, "status", fields.get("status"), "active", "unknown"),
                severity=_enum(Condition, "severity", fields.get("severity"), None),
                extraction_region=region,
                extraction_confidence=confidence,
                clinician_confirmed=True,
                clinician_confirmed_at=datetime.now(UTC),
            )
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
        # Normalised before the resolve decision below, not just before the INSERT: an
        # unreadable allergen type falls back to "drug", which is the reading that gets
        # cross-checked against the vocabulary rather than the one that skips the check.
        allergen_type = _enum(Allergy, "allergen_type", fields.get("allergen_type"), "drug")
        vocab_id = None
        if allergen_type == "drug":
            resolved = await self.resolver.resolve(name)
            vocab_id = resolved.vocabulary_id if resolved else None
        allergy = Allergy(
            **_fitted(
                Allergy,
                patient_id=patient.id,
                source_document_id=source_doc_id,
                allergen_name=name,
                allergen_type=allergen_type,
                reaction_description=fields.get("reaction_description"),
                severity=_enum(Allergy, "severity", fields.get("severity"), None),
                status="active",
                drug_vocabulary_id=vocab_id,
                extraction_region=region,
                extraction_confidence=confidence,
                clinician_confirmed=True,
                clinician_confirmed_at=datetime.now(UTC),
            )
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
