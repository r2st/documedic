"""Drug-safety service (P1-08): loads patient + reference data, runs the deterministic
engine in app.core.safety, persists append-only results, and audits the check.

Fully offline-capable: no LLM, no network. Allergy conflicts and absolute contraindications
are hard blocks that cannot be dismissed.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safety import (
    ContraindicationRule,
    DrugRef,
    InteractionRule,
    PatientAllergy,
    PatientCondition,
    SafetyContext,
    SafetyFlag,
    evaluate_drug_safety,
    has_hard_block,
)
from app.exceptions import NotFoundError, PatientNotFoundError, ValidationError
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.drug_safety_check import DrugSafetyCheck
from app.models.drug_safety_override import DrugSafetyOverride
from app.models.drug_vocabulary import Contraindication, DrugInteraction, DrugVocabulary
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.services.audit_service import AuditService
from app.services.drug_resolver import DrugResolver


class SafetyService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)
        self.resolver = DrugResolver(db)

    async def _patient(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
        result = await self.db.execute(
            select(Patient).where(
                Patient.id == patient_id,
                Patient.account_id == account_id,
                Patient.is_deleted.is_(False),
            )
        )
        patient = result.scalar_one_or_none()
        if patient is None:
            raise PatientNotFoundError()
        return patient

    async def _build_context(
        self, patient_id: uuid.UUID, *, exclude_reference_id: str | None = None
    ) -> SafetyContext:
        # Current medications, resolved to reference ids + classes.
        meds_result = await self.db.execute(
            select(MedicationEvent).where(
                MedicationEvent.patient_id == patient_id,
                MedicationEvent.is_deleted.is_(False),
                MedicationEvent.is_current.is_(True),
            )
        )
        current_meds: list[DrugRef] = []
        vocab_by_id: dict = {}
        for med in meds_result.scalars().all():
            ref_id, generic, drug_class = None, med.generic_name, None
            if med.drug_vocabulary_id:
                vocab = vocab_by_id.get(med.drug_vocabulary_id) or await self.db.get(
                    DrugVocabulary, med.drug_vocabulary_id
                )
                if vocab:
                    vocab_by_id[med.drug_vocabulary_id] = vocab
                    ref_id, generic, drug_class = (
                        vocab.reference_id,
                        vocab.generic_name,
                        vocab.drug_class,
                    )
            if ref_id is None and med.generic_name:
                resolved = await self.resolver.resolve(med.generic_name)
                if resolved:
                    ref_id, generic, drug_class = (
                        resolved.reference_id,
                        resolved.generic_name,
                        resolved.drug_class,
                    )
            if ref_id and ref_id != exclude_reference_id:
                current_meds.append(
                    DrugRef(reference_id=ref_id, generic_name=generic or "", drug_class=drug_class)
                )

        # Allergies (drug allergies resolved to reference id + class).
        allergies_result = await self.db.execute(
            select(Allergy).where(
                Allergy.patient_id == patient_id,
                Allergy.is_deleted.is_(False),
                Allergy.status == "active",
            )
        )
        allergies: list[PatientAllergy] = []
        for a in allergies_result.scalars().all():
            ref_id, drug_class = None, None
            if a.drug_vocabulary_id:
                vocab = await self.db.get(DrugVocabulary, a.drug_vocabulary_id)
                if vocab:
                    ref_id, drug_class = vocab.reference_id, vocab.drug_class
            elif a.allergen_type == "drug":
                resolved = await self.resolver.resolve(a.allergen_name)
                if resolved:
                    ref_id, drug_class = resolved.reference_id, resolved.drug_class
            allergies.append(
                PatientAllergy(
                    allergen_name=a.allergen_name,
                    drug_reference_id=ref_id,
                    drug_class=drug_class,
                    allergy_id=str(a.id),
                )
            )

        # Conditions.
        cond_result = await self.db.execute(
            select(Condition).where(
                Condition.patient_id == patient_id,
                Condition.is_deleted.is_(False),
                Condition.status == "active",
            )
        )
        conditions = [
            PatientCondition(condition_name=c.condition_name, icd10_code=c.icd10_code)
            for c in cond_result.scalars().all()
        ]

        # Latest eGFR.
        egfr_result = await self.db.execute(
            select(DerivedMarker)
            .where(
                DerivedMarker.patient_id == patient_id,
                DerivedMarker.marker_name == "eGFR",
                DerivedMarker.is_deleted.is_(False),
            )
            .order_by(DerivedMarker.computed_at.desc())
            .limit(1)
        )
        egfr_marker = egfr_result.scalar_one_or_none()
        egfr = float(egfr_marker.value_numeric) if egfr_marker else None

        # Reference data (interactions touching current meds + the proposed drug; all CIs).
        interactions = await self._load_interactions()
        contraindications = await self._load_contraindications()

        return SafetyContext(
            current_meds=current_meds,
            allergies=allergies,
            conditions=conditions,
            egfr=egfr,
            interaction_rules=interactions,
            contraindication_rules=contraindications,
        )

    async def _load_interactions(self) -> list[InteractionRule]:
        result = await self.db.execute(
            select(DrugInteraction).where(DrugInteraction.is_active.is_(True))
        )
        return [
            InteractionRule(
                drug_a_reference_id=r.drug_a_reference_id,
                drug_b_reference_id=r.drug_b_reference_id,
                severity=r.severity,
                description=r.description,
                management=r.management,
                interaction_id=str(r.id),
            )
            for r in result.scalars().all()
        ]

    async def _load_contraindications(self) -> list[ContraindicationRule]:
        result = await self.db.execute(
            select(Contraindication).where(Contraindication.is_active.is_(True))
        )
        return [
            ContraindicationRule(
                drug_reference_id=r.drug_reference_id,
                condition_name=r.condition_name,
                severity=r.severity,
                description=r.description,
                is_absolute=r.is_absolute,
                renal_threshold=r.renal_threshold,
                hepatic_threshold=r.hepatic_threshold,
                contraindication_id=str(r.id),
            )
            for r in result.scalars().all()
        ]

    async def check_medication(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        drug_reference_id: str | None,
        drug_name: str | None,
    ) -> tuple[DrugVocabulary, SafetyContext, list[SafetyFlag], list[uuid.UUID]]:
        await self._patient(account_id, patient_id)

        vocab: DrugVocabulary | None = None
        if drug_reference_id:
            vocab = await self.resolver.resolve_reference_id(drug_reference_id)
        if vocab is None and drug_name:
            resolved = await self.resolver.resolve(drug_name)
            if resolved:
                vocab = await self.resolver.resolve_reference_id(resolved.reference_id)
        if vocab is None:
            raise ValidationError("Could not resolve the proposed drug via DrugVocabulary")

        # Deliberately NOT excluding the proposed drug's own reference id from current_meds
        # here (unlike active_flags' pairwise sub_ctx below): check_duplicate_therapy needs to
        # see it to detect "patient is already on this exact product". check_interactions
        # already self-skips (med.reference_id == proposed.reference_id), so nothing else in
        # evaluate_drug_safety depends on the exclusion.
        ctx = await self._build_context(patient_id)
        proposed = DrugRef(
            reference_id=vocab.reference_id,
            generic_name=vocab.generic_name,
            drug_class=vocab.drug_class,
        )
        flags = evaluate_drug_safety(proposed, ctx)
        check_ids = await self._persist(account_id, patient_id, vocab, flags)
        return vocab, ctx, flags, check_ids

    async def _persist(
        self,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        vocab: DrugVocabulary,
        flags: list[SafetyFlag],
    ) -> list[uuid.UUID]:
        rows = [
            DrugSafetyCheck(
                patient_id=patient_id,
                account_id=account_id,
                drug_vocabulary_id=vocab.id,
                check_type=flag.check_type,
                severity=flag.severity,
                is_hard_block=flag.is_hard_block,
                summary=flag.summary,
                details=flag.details,
                drug_interaction_id=_to_uuid(flag.drug_interaction_id),
                contraindication_id=_to_uuid(flag.contraindication_id),
                allergy_id=_to_uuid(flag.allergy_id),
            )
            for flag in flags
        ]
        for row in rows:
            self.db.add(row)
        await self.db.flush()
        await self.audit.record(
            action="drug_safety_check",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="drug_vocabulary",
            entity_id=vocab.id,
            payload={
                "drug": vocab.generic_name,
                "reference_id": vocab.reference_id,
                "flag_count": len(flags),
                "hard_block": has_hard_block(flags),
            },
        )
        await self.db.commit()
        return [row.id for row in rows]

    async def override_hard_block(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        drug_safety_check_id: uuid.UUID,
        reasoning: str,
    ) -> DrugSafetyOverride:
        """Record a clinician's documented override of a hard-blocked check.

        Does not delete or mutate the original DrugSafetyCheck (append-only, rule #7 spirit) --
        it adds a separate, linked, equally immutable override record and audits the action.
        The hard block itself is never silently bypassed: this is the only sanctioned path
        past one, and it always requires non-trivial documented reasoning (rule #3).
        """
        await self._patient(account_id, patient_id)
        check = await self.db.get(DrugSafetyCheck, drug_safety_check_id)
        if check is None or check.patient_id != patient_id:
            raise NotFoundError("Drug safety check not found")
        if not check.is_hard_block:
            raise ValidationError("Only hard-blocked checks require an override")

        override = DrugSafetyOverride(
            account_id=account_id,
            patient_id=patient_id,
            drug_vocabulary_id=check.drug_vocabulary_id,
            drug_safety_check_id=check.id,
            reasoning=reasoning.strip(),
        )
        self.db.add(override)
        await self.db.flush()
        await self.audit.record(
            action="drug_safety_hard_block_overridden",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="drug_safety_check",
            entity_id=check.id,
            payload={
                "check_type": check.check_type,
                "summary": check.summary,
                "reasoning": override.reasoning,
            },
        )
        await self.db.commit()
        await self.db.refresh(override)
        return override

    async def list_overrides(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID
    ) -> list[DrugSafetyOverride]:
        await self._patient(account_id, patient_id)
        result = await self.db.execute(
            select(DrugSafetyOverride)
            .where(DrugSafetyOverride.patient_id == patient_id)
            .order_by(DrugSafetyOverride.created_at.desc())
        )
        return list(result.scalars().all())

    async def active_flags(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID
    ) -> list[tuple[DrugVocabulary, list[SafetyFlag]]]:
        """Re-run pairwise checks across all current medications (P1-08c GET flags)."""
        await self._patient(account_id, patient_id)
        ctx = await self._build_context(patient_id)
        out: list[tuple[DrugVocabulary, list[SafetyFlag]]] = []
        seen_refs = {m.reference_id for m in ctx.current_meds}
        for ref in seen_refs:
            vocab = await self.resolver.resolve_reference_id(ref)
            if vocab is None:
                continue
            sub_ctx = await self._build_context(patient_id, exclude_reference_id=ref)
            proposed = DrugRef(
                reference_id=vocab.reference_id,
                generic_name=vocab.generic_name,
                drug_class=vocab.drug_class,
            )
            flags = evaluate_drug_safety(proposed, sub_ctx)
            if flags:
                out.append((vocab, flags))
        return out


def _to_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None
