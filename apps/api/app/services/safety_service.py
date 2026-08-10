"""Drug-safety service (P1-08): loads patient + reference data, runs the deterministic
engine in app.core.safety, persists append-only results, and audits the check.

Fully offline-capable: no LLM, no network. Allergy conflicts and absolute contraindications
are hard blocks that cannot be dismissed.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

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
from app.exceptions import NotFoundError, ValidationError
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
        """Fetch a patient the caller owns, or raise PatientNotFoundError.

        Delegates rather than repeating the predicate. This is an authorization check -- it is
        what stops one account reading another's records -- and it was previously copy-pasted
        into four services. Any future change to what "a patient this caller may read" means
        (an extra tenancy dimension, an account-status check) has to land in one place or it
        lands in three and misses the fourth.

        Imported inside the method: PatientService imports from this module's siblings, so a
        module-level import would close a cycle. Same pattern as DocumentService._get_patient.
        """
        from app.services.patient_service import PatientService

        return await PatientService(self.db).get(account_id, patient_id)

    async def _build_context(
        self, patient_id: uuid.UUID, *, proposed_reference_id: str | None = None
    ) -> SafetyContext:
        """Assemble everything ``evaluate_drug_safety`` needs for one patient.

        ``proposed_reference_id`` is the drug about to be checked, when there is one. It only
        widens the *reference-data* scope: the two rule tables are loaded for the drugs actually
        in play (current medications plus the proposal) rather than in full. Patient data is
        unaffected by it.
        """
        med_rows = await self._current_medication_rows(patient_id)
        allergy_rows = await self._active_allergy_rows(patient_id)
        vocab_by_id = await self._vocabulary_by_id(med_rows, allergy_rows)

        current_meds = await self._current_meds(med_rows, vocab_by_id)
        allergies = await self._allergies(allergy_rows, vocab_by_id)

        # Reference data, scoped to the drugs this evaluation can possibly involve. Interactions
        # only fire between the proposal and a current medication, and contraindications only for
        # the proposal itself, so a full-table load is wasted I/O that grows with the corpus
        # rather than with the patient. Both predicates ride existing indexes.
        reference_ids = {m.reference_id for m in current_meds}
        if proposed_reference_id:
            reference_ids.add(proposed_reference_id)

        return SafetyContext(
            current_meds=current_meds,
            allergies=allergies,
            conditions=await self._conditions(patient_id),
            egfr=await self._latest_egfr(patient_id),
            interaction_rules=await self._load_interactions(reference_ids),
            contraindication_rules=await self._load_contraindications(reference_ids),
        )

    async def _current_medication_rows(self, patient_id: uuid.UUID) -> list[MedicationEvent]:
        result = await self.db.execute(
            select(MedicationEvent).where(
                MedicationEvent.patient_id == patient_id,
                MedicationEvent.is_deleted.is_(False),
                MedicationEvent.is_current.is_(True),
            )
        )
        return list(result.scalars().all())

    async def _active_allergy_rows(self, patient_id: uuid.UUID) -> list[Allergy]:
        result = await self.db.execute(
            select(Allergy).where(
                Allergy.patient_id == patient_id,
                Allergy.is_deleted.is_(False),
                Allergy.status == "active",
            )
        )
        return list(result.scalars().all())

    async def _vocabulary_by_id(
        self, med_rows: list[MedicationEvent], allergy_rows: list[Allergy]
    ) -> dict[uuid.UUID, DrugVocabulary]:
        """Batch-load every vocabulary row the two builders need, in one query.

        This used to be a ``db.get()`` per medication and per allergy — on the hot deterministic
        safety path, so a patient on 10 drugs paid 10 extra round-trips per check.
        """
        vocab_ids = {row.drug_vocabulary_id for row in med_rows if row.drug_vocabulary_id} | {
            row.drug_vocabulary_id for row in allergy_rows if row.drug_vocabulary_id
        }
        if not vocab_ids:
            return {}
        result = await self.db.execute(
            select(DrugVocabulary).where(DrugVocabulary.id.in_(vocab_ids))
        )
        return {vocab.id: vocab for vocab in result.scalars().all()}

    async def _current_meds(
        self, med_rows: list[MedicationEvent], vocab_by_id: dict[uuid.UUID, DrugVocabulary]
    ) -> list[DrugRef]:
        out: list[DrugRef] = []
        for med in med_rows:
            vocab = vocab_by_id.get(med.drug_vocabulary_id) if med.drug_vocabulary_id else None
            if vocab is not None:
                out.append(
                    DrugRef(
                        reference_id=vocab.reference_id,
                        generic_name=vocab.generic_name,
                        drug_class=vocab.drug_class,
                    )
                )
                continue
            # Unlinked row (e.g. imported before the vocabulary knew the brand): fall back to
            # name resolution so the drug still participates in the safety evaluation.
            resolved = await self.resolver.resolve(med.generic_name)
            if resolved:
                out.append(
                    DrugRef(
                        reference_id=resolved.reference_id,
                        generic_name=resolved.generic_name,
                        drug_class=resolved.drug_class,
                    )
                )
        return out

    async def _allergies(
        self, allergy_rows: list[Allergy], vocab_by_id: dict[uuid.UUID, DrugVocabulary]
    ) -> list[PatientAllergy]:
        out: list[PatientAllergy] = []
        for a in allergy_rows:
            ref_id, drug_class = None, None
            vocab = vocab_by_id.get(a.drug_vocabulary_id) if a.drug_vocabulary_id else None
            if vocab is not None:
                ref_id, drug_class = vocab.reference_id, vocab.drug_class
            elif a.drug_vocabulary_id is None and a.allergen_type == "drug":
                resolved = await self.resolver.resolve(a.allergen_name)
                if resolved:
                    ref_id, drug_class = resolved.reference_id, resolved.drug_class
            out.append(
                PatientAllergy(
                    allergen_name=a.allergen_name,
                    drug_reference_id=ref_id,
                    drug_class=drug_class,
                    allergy_id=str(a.id),
                )
            )
        return out

    async def _conditions(self, patient_id: uuid.UUID) -> list[PatientCondition]:
        result = await self.db.execute(
            select(Condition).where(
                Condition.patient_id == patient_id,
                Condition.is_deleted.is_(False),
                Condition.status == "active",
            )
        )
        return [
            PatientCondition(condition_name=c.condition_name, icd10_code=c.icd10_code)
            for c in result.scalars().all()
        ]

    async def _latest_egfr(self, patient_id: uuid.UUID) -> float | None:
        result = await self.db.execute(
            select(DerivedMarker)
            .where(
                DerivedMarker.patient_id == patient_id,
                DerivedMarker.marker_name == "eGFR",
                DerivedMarker.is_deleted.is_(False),
            )
            .order_by(DerivedMarker.computed_at.desc())
            .limit(1)
        )
        marker = result.scalar_one_or_none()
        return float(marker.value_numeric) if marker else None

    async def _load_interactions(self, reference_ids: set[str]) -> list[InteractionRule]:
        """Interaction rules whose *both* endpoints are drugs in play.

        A rule with only one endpoint in the set can never fire — ``check_interactions`` looks
        up the (proposed, current-med) pair — so fetching it is pure waste. Fewer than two drugs
        means no pair exists at all.
        """
        if len(reference_ids) < 2:
            return []
        result = await self.db.execute(
            select(DrugInteraction).where(
                DrugInteraction.is_active.is_(True),
                DrugInteraction.drug_a_reference_id.in_(reference_ids),
                DrugInteraction.drug_b_reference_id.in_(reference_ids),
            )
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

    async def _load_contraindications(self, reference_ids: set[str]) -> list[ContraindicationRule]:
        """Contraindication rules for the drugs in play.

        ``check_contraindications`` discards every rule whose ``drug_reference_id`` is not the
        proposed drug, so the filter belongs in the query.
        """
        if not reference_ids:
            return []
        result = await self.db.execute(
            select(Contraindication).where(
                Contraindication.is_active.is_(True),
                Contraindication.drug_reference_id.in_(reference_ids),
            )
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

        # The proposed drug's own reference id stays in current_meds if the patient is already
        # on it: check_duplicate_therapy needs to see it to detect "already an active order for
        # this exact product". check_interactions self-skips that pair, so nothing else in
        # evaluate_drug_safety is affected.
        ctx = await self._build_context(patient_id, proposed_reference_id=vocab.reference_id)
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
        """Re-run pairwise checks across all current medications (P1-08c GET flags).

        The context is built ONCE and each drug's "everyone but me" variant is derived in
        memory by dropping that drug from ``current_meds``. Allergies, conditions, eGFR and the
        two reference tables are identical for every drug in the loop, so a per-drug rebuild
        re-ran the same queries N times for N current medications.

        No ``proposed_reference_id`` is passed: every drug evaluated here is already a current
        medication, so the reference-data scope is exactly the current-medication set.
        """
        await self._patient(account_id, patient_id)
        ctx = await self._build_context(patient_id)
        out: list[tuple[DrugVocabulary, list[SafetyFlag]]] = []
        seen_refs = {m.reference_id for m in ctx.current_meds}
        for ref in sorted(seen_refs):
            vocab = await self.resolver.resolve_reference_id(ref)
            if vocab is None:
                continue
            sub_ctx = replace(
                ctx, current_meds=[m for m in ctx.current_meds if m.reference_id != ref]
            )
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
