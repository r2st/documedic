"""Drug-safety service (P1-08): loads patient + reference data, runs the deterministic
engine in app.core.safety, persists append-only results, and audits the check.

Fully offline-capable: no LLM, no network. Allergy conflicts and absolute contraindications
are hard blocks that cannot be dismissed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clinical import age_from_dob, serum_creatinine_mg_dl
from app.core.dates import is_plausible_clinical_date
from app.core.lab_safety import canonical_lab_value
from app.core.safety import (
    ContraindicationRule,
    DrugRef,
    HepaticPanel,
    InteractionRule,
    PatientAllergy,
    PatientCondition,
    SafetyContext,
    SafetyFlag,
    check_hepatic_severity,
    check_unevaluated_allergies,
    check_unevaluated_conditions,
    check_unevaluated_medications,
    evaluate_drug_safety,
    has_hard_block,
    ingredient_reference_ids,
)
from app.exceptions import NotFoundError, ValidationError
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.drug_safety_check import DrugSafetyCheck
from app.models.drug_safety_override import DrugSafetyOverride
from app.models.drug_vocabulary import Contraindication, DrugInteraction, DrugVocabulary
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.services.audit_service import AuditService
from app.services.drug_resolver import DrugResolver, ResolvedDrug


def _components(raw: object) -> tuple[DrugRef, ...]:
    """The curated ingredient list on a vocabulary row, as ``DrugRef``s.

    Defensive about the payload's shape because this is on the deterministic safety path: a
    malformed row must cost the engine that row's ingredients, not the whole check. An entry
    with no ``generic_name`` is dropped — a nameless ingredient matches nothing and would only
    put a blank into a flag's text.

    A component with no ``reference_id`` of its own gets an empty one rather than being dropped.
    Clavulanic acid and hydrochlorothiazide are real molecules with no standalone row in this
    vocabulary; they match no reference-id-keyed rule either way, but they do match a documented
    allergy by name or class, and dropping them would lose that.
    """
    if not isinstance(raw, list):
        return ()
    out: list[DrugRef] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("generic_name")
        if not isinstance(name, str) or not name.strip():
            continue
        reference_id = entry.get("reference_id")
        drug_class = entry.get("drug_class")
        out.append(
            DrugRef(
                reference_id=reference_id if isinstance(reference_id, str) else "",
                generic_name=name.strip(),
                drug_class=drug_class if isinstance(drug_class, str) else None,
            )
        )
    return tuple(out)


def _drug_ref(row: DrugVocabulary | ResolvedDrug) -> DrugRef:
    """A vocabulary row (however it was reached) as the safety engine's drug identity.

    One place, because a ``DrugRef`` built without ``components`` silently un-does the whole
    fixed-dose-combination pass: the checks would see Glycomet GP as one opaque product again
    and match it against none of metformin's rules. Four call sites built this by hand.
    """
    return DrugRef(
        reference_id=row.reference_id,
        generic_name=row.generic_name,
        drug_class=row.drug_class,
        hepatotoxicity=row.hepatotoxicity,
        components=_components(row.components),
    )


class SafetyService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)
        self.resolver = DrugResolver(db)
        # The patient-scoped half of a safety context, per patient, for this instance's
        # lifetime. Same "one unit of work" contract as the DrugResolver above, and for the
        # same reason -- see ``_patient_facts``.
        self._facts: dict[uuid.UUID, SafetyContext] = {}

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
        self, patient_id: uuid.UUID, *, proposed_reference_ids: Iterable[str] = ()
    ) -> SafetyContext:
        """Assemble everything ``evaluate_drug_safety`` needs for one patient.

        ``proposed_reference_ids`` are the drugs about to be checked, when there are any. They
        only widen the *reference-data* scope: the two rule tables are loaded for the drugs
        actually in play (current medications plus the proposals) rather than in full. Patient
        data is unaffected by them.

        Plural because a management option names however many drugs its guideline names — "an
        ACE inhibitor or ARB (e.g. enalapril/telmisartan)" is one option and two molecules — and
        a rule for a drug outside this set is a rule that cannot fire, which is how a
        contraindication for a proposed drug would go silently unloaded.
        """
        facts = await self._patient_facts(patient_id)

        # Reference data, scoped to the drugs this evaluation can possibly involve. Interactions
        # only fire between the proposal and a current medication, and contraindications only for
        # the proposal itself, so a full-table load is wasted I/O that grows with the corpus
        # rather than with the patient. Both predicates ride existing indexes.
        #
        # "The drugs in play" means every molecule, not every product: a combination is
        # evaluated as the ingredients it contains, so scoping the query to the product's own
        # reference id loads none of its ingredients' rules and the checks then find nothing to
        # apply. The proposals are widened by the caller, which has the resolved rows; the
        # current medications are widened here.
        reference_ids = {
            ref for med in facts.current_meds for ref in ingredient_reference_ids(med)
        } | {ref for ref in proposed_reference_ids if ref}

        return replace(
            facts,
            interaction_rules=await self._load_interactions(reference_ids),
            contraindication_rules=await self._load_contraindications(reference_ids),
        )

    async def _patient_facts(self, patient_id: uuid.UUID) -> SafetyContext:
        """The patient half of a safety context: meds, allergies, conditions, eGFR.

        A ``SafetyContext`` with the two rule lists left empty, memoised per patient for this
        service instance's lifetime; :meth:`_build_context` ``replace``s the rules onto a copy.

        Split out because these five reads do not depend on the drug being evaluated, while
        ``screen_text`` is called once per management option a run produced. Every option was
        re-reading the same current medications, active allergies, their vocabulary rows, the
        conditions and the latest eGFR — for a chart that cannot change between two options of
        the same run — which put five avoidable round-trips per option in front of a clinician
        watching a progress spinner.

        Memoising on the instance rather than passing the facts down is what keeps the seam:
        every caller still asks for a whole context and cannot accidentally evaluate against a
        half-built one. The lifetime is a request or a reasoning run (every construction site
        builds a fresh service), which is the same contract the embedded ``DrugResolver``
        already documents for its own caches.
        """
        if patient_id not in self._facts:
            med_rows = await self._current_medication_rows(patient_id)
            allergy_rows = await self._active_allergy_rows(patient_id)
            vocab_by_id = await self._vocabulary_by_id(med_rows, allergy_rows)
            current_meds, unresolved = await self._current_meds(med_rows, vocab_by_id)
            allergies, unidentified_allergies = await self._allergies(allergy_rows, vocab_by_id)
            self._facts[patient_id] = SafetyContext(
                current_meds=current_meds,
                unresolved_current_meds=unresolved,
                allergies=allergies,
                unresolved_allergies=unidentified_allergies,
                conditions=await self._conditions(patient_id),
                egfr=await self._latest_egfr(patient_id),
                hepatic=await self._hepatic_panel(patient_id),
                age_years=await self._age_years(patient_id),
            )
        return self._facts[patient_id]

    async def _age_years(self, patient_id: uuid.UUID) -> int | None:
        """The patient's age in whole years today, or None when the record cannot say.

        None for a missing date of birth and for one that is not a date this patient could have
        been born on — an OCR'd "2126" or a DOB in the future. ``check_geriatric_cautions``
        reports a None as a check that did not run rather than as a check that passed, so the
        honest answer here is the safe one; deriving an age from an implausible date would put a
        confident number underneath a clinical caution.
        """
        row = await self.db.execute(select(Patient.date_of_birth).where(Patient.id == patient_id))
        dob = row.scalars().first()
        if dob is None or not is_plausible_clinical_date(dob):
            return None
        age = age_from_dob(dob, datetime.now(UTC).date())
        return age if age >= 0 else None

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
                # "unknown" alongside "active": the four permitted statuses are active,
                # resolved, refuted and unknown, and only the middle two are the chart saying
                # this allergy is not a live concern. An allergy nobody has been able to
                # confirm is the one a hard block exists for, and reading it as absent is the
                # same fail-open shape as an allergen the vocabulary cannot identify.
                Allergy.status.in_(("active", "unknown")),
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
    ) -> tuple[list[DrugRef], list[str]]:
        """The chart's current medications as evaluable drugs, and the names that are not.

        The second half is not bookkeeping. A row that resolves to nothing is absent from every
        rule evaluated against ``current_meds``, and until it was carried out of here that
        absence was indistinguishable from there being nothing to find — see
        ``check_unevaluated_medications``, which is what turns it back into something the
        clinician is told.
        """
        # Rows with no vocabulary link fall through to name resolution below. Resolving them
        # one at a time is a query per unlinked medication; prefetching makes it one for all.
        await self.resolver.prefetch(
            med.generic_name for med in med_rows if not med.drug_vocabulary_id
        )
        out: list[DrugRef] = []
        unresolved: list[str] = []
        for med in med_rows:
            vocab = vocab_by_id.get(med.drug_vocabulary_id) if med.drug_vocabulary_id else None
            if vocab is not None:
                out.append(_drug_ref(vocab))
                continue
            # Unlinked row (e.g. imported before the vocabulary knew the brand): fall back to
            # name resolution so the drug still participates in the safety evaluation.
            resolved = await self.resolver.resolve(med.generic_name)
            if resolved:
                out.append(_drug_ref(resolved))
            elif med.generic_name and med.generic_name.strip():
                unresolved.append(med.generic_name.strip())
        return out, unresolved

    async def _allergies(
        self, allergy_rows: list[Allergy], vocab_by_id: dict[uuid.UUID, DrugVocabulary]
    ) -> tuple[list[PatientAllergy], list[str]]:
        """The chart's allergies as evaluable ones, and the drug allergens that are not.

        The second half is the allergy counterpart of ``_current_meds``' unresolved list, and it
        matters more. An allergen with no reference id and no drug class reduces
        ``check_allergies`` to comparing the raw charted text against the proposed drug's INN,
        so a documented allergy to an unseeded brand contributes nothing at all — see
        ``check_unevaluated_allergies``.
        """
        await self.resolver.prefetch(
            a.allergen_name
            for a in allergy_rows
            if a.drug_vocabulary_id is None and a.allergen_type == "drug"
        )
        out: list[PatientAllergy] = []
        unidentified: list[str] = []
        for a in allergy_rows:
            ref_id, drug_class = None, None
            vocab = vocab_by_id.get(a.drug_vocabulary_id) if a.drug_vocabulary_id else None
            if vocab is not None:
                ref_id, drug_class = vocab.reference_id, vocab.drug_class
            elif a.drug_vocabulary_id is None and a.allergen_type == "drug":
                resolved = await self.resolver.resolve(a.allergen_name)
                if resolved:
                    ref_id, drug_class = resolved.reference_id, resolved.drug_class
                elif a.allergen_name and a.allergen_name.strip():
                    unidentified.append(a.allergen_name.strip())
            out.append(
                PatientAllergy(
                    allergen_name=a.allergen_name,
                    drug_reference_id=ref_id,
                    drug_class=drug_class,
                    allergy_id=str(a.id),
                )
            )
        return out, unidentified

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
        """The patient's current eGFR — the one derived from the most recent blood draw.

        Ordered by the *sample date of the source creatinine*, not by ``computed_at``.
        ``computed_at`` is when the CKD-EPI arithmetic ran, which is ingestion time: every
        marker on a document approved today carries today's timestamp no matter when the blood
        was drawn. This product exists to read fragmented histories, and a patient's reports are
        uploaded in whatever order they come out of the folder, so "today's upload is a report
        from 2019" is the ordinary case rather than a contrived one.

        Sorting on that put a seven-year-old eGFR of 95 ahead of last month's 22 and handed it
        to ``_evaluate_renal``, which is the only input to the metformin hard block below 30 —
        so the block did not fire, and the response said ``is_blocked: false`` for a patient
        whose current renal function is in the chart. Nothing about the answer showed which
        measurement it was computed from. ``LabResult`` has ordered by ``sample_date`` on both
        its readers since it was written; this is the derived side agreeing with it.

        ``coalesce`` rather than a ``NULLS LAST``: sample dates come out of OCR and are
        routinely missing, and for a row with nothing else to sort on, when it was computed is
        still the best proxy available. Excluding those rows would drop a real measurement from
        the safety check to avoid mis-ranking it, which is the worse trade. ``computed_at``
        breaks ties (two draws that share a date), and the id makes the result stable rather
        than leaving a tie to the planner.
        """
        observed_at = func.coalesce(LabResult.sample_date, DerivedMarker.computed_at)
        result = await self.db.execute(
            select(DerivedMarker.value_numeric)
            .outerjoin(LabResult, LabResult.id == DerivedMarker.source_lab_result_id)
            .where(
                DerivedMarker.patient_id == patient_id,
                DerivedMarker.marker_name == "eGFR",
                DerivedMarker.is_deleted.is_(False),
            )
            .order_by(observed_at.desc(), DerivedMarker.computed_at.desc(), DerivedMarker.id)
            .limit(1)
        )
        value = result.scalars().first()
        return float(value) if value is not None else None

    async def _hepatic_panel(self, patient_id: uuid.UUID) -> HepaticPanel:
        """The chart's current liver function, for the hepatic dose-adjustment thresholds.

        The most recent value per marker, in the canonical unit — the same ordering argument as
        ``_latest_egfr`` (sample date, not ingestion time, because this product reads histories
        uploaded in whatever order the folder produced them) and the same refusal to guess as
        everywhere else: a row this cannot identify or convert is left out, which makes the
        panel empty, which makes ``_evaluate_hepatic`` report itself unevaluated rather than
        pass. Both markers come from one query; the narrowing predicate is the marker-name
        prefix set, and the rows are few enough per patient that the conversion happens here.
        """
        # The predicate narrows; ``canonical_lab_value`` decides. A patient's lab history is
        # unbounded — ``LabSafetyService`` was rewritten once already for reading all of it to
        # use a handful of rows — so the marker names are filtered in SQL rather than in
        # Python. Deliberately loose (a substring, not the alias list): the alias table matches
        # on a normalised form the database cannot compute, so SQL's job here is only to keep
        # the transfer proportional to the liver panel instead of to the whole chart.
        name = func.lower(LabResult.marker_name)
        result = await self.db.execute(
            select(LabResult.marker_name, LabResult.value_numeric, LabResult.unit)
            .where(
                LabResult.patient_id == patient_id,
                LabResult.is_deleted.is_(False),
                LabResult.value_numeric.is_not(None),
                or_(
                    name.like("%bilirubin%"),
                    name.like("%alt%"),
                    name.like("%ast%"),
                    name.like("%sgpt%"),
                    name.like("%sgot%"),
                    name.like("%transaminase%"),
                    name.like("%aminotransferase%"),
                    # The further Child-Pugh and MELD inputs. Loose for the same reason as the
                    # rest: these bring in urine albumins and creatinine clearances too, and the
                    # normalisers below are what refuse them.
                    name.like("%albumin%"),
                    name.like("%alb%"),
                    name.like("%inr%"),
                    name.like("%creatinin%"),
                    name.like("%creat%"),
                ),
            )
            .order_by(
                LabResult.sample_date.desc().nullslast(),
                LabResult.created_at.desc(),
                LabResult.id,
            )
        )
        latest: dict[str, float] = {}
        creatinine: float | None = None
        for marker_name, value_numeric, unit in result.all():
            # The creatinine goes through the *serum* predicate, not the general normaliser. A
            # urine creatinine and a creatinine clearance are different analytes on different
            # scales, and feeding either into MELD is R44's bug ("a urine creatinine was computed
            # into an eGFR") in a second place. First row wins here too.
            if creatinine is None:
                creatinine = serum_creatinine_mg_dl(marker_name, value_numeric, unit)
            canonical = canonical_lab_value(marker_name, float(value_numeric), unit)
            # First row wins: the query is already newest-first, so a later row for the same
            # marker is an older draw.
            if canonical is not None and canonical[0] not in latest:
                latest[canonical[0]] = canonical[1]
        return HepaticPanel(
            bilirubin_mg_dl=latest.get("bilirubin"),
            alt_u_l=latest.get("alt"),
            albumin_g_dl=latest.get("albumin"),
            inr=latest.get("inr"),
            creatinine_mg_dl=creatinine,
        )

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
                # The second axis condition matching runs on. A code equality is unambiguous
                # where a name comparison is a guess about wording, so it is carried whenever
                # both sides have one; see app.core.safety._match_condition.
                icd10_code=r.icd10_code,
            )
            for r in result.scalars().all()
        ]

    async def _reject_if_multiple_drugs(self, drug_name: str) -> None:
        """Refuse a proposal that names more than one drug, instead of checking one of them.

        ``DrugResolver`` scores with ``fuzz.WRatio``, whose partial-ratio path scores 90 — above
        the 86 acceptance threshold — whenever the query merely *contains* a candidate name.
        That is deliberate and load-bearing: "Augmentin Duo 625 Tablet" and "Tab. Crocin 500 BD
        x 5 days" are exactly what a clinician types and what OCR lifts off a prescription, and
        both must resolve through the trade name to the INN.

        The same leniency turned a two-drug string into a one-drug answer. "Warfarin, Aspirin"
        resolved to Aspirin at 90 and the endpoint replied ``is_blocked: false`` — for the one
        pair that is the textbook major interaction. The warfarin the clinician typed was never
        evaluated, and nothing in the verdict said so. Every worse variant of that answer is
        reachable the same way: any second drug in the string is dropped, and dropping the
        allergen is how a hard block silently becomes a clean bill of health.

        So ambiguity is refused rather than resolved, which is the rule this module already
        applies to a low fuzzy score: an unresolved drug is excluded from evaluation, and a wrong
        guess is worse than no answer (CLAUDE.md pitfall #4). Picking one of two named drugs is a
        guess, and it is the kind that reads as a completed check.

        Only at this boundary — a clinician proposing a drug and getting a verdict about that
        input. Names already in the record go through ``resolve`` untouched; see
        ``DrugResolver.drugs_named_in`` for why refusing there would make things worse.
        """
        named = await self.resolver.drugs_named_in(drug_name)
        if len(named) < 2:
            return
        listed = ", ".join(sorted(named.values()))
        raise ValidationError(
            f"“{drug_name}” names more than one drug ({listed}), so no safety check was run — "
            "this is not the same as “no interactions found”. Check one proposed medication at "
            "a time: a single check evaluates a single drug, and checking this as one would have "
            "reported on only one of them.",
            detail=(
                f"ambiguous proposal name={drug_name!r} matched "
                f"{sorted(named)} — refused rather than resolved to one"
            ),
        )

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
            await self._reject_if_multiple_drugs(drug_name)
            resolved = await self.resolver.resolve(drug_name)
            if resolved:
                vocab = await self.resolver.resolve_reference_id(resolved.reference_id)
        if vocab is None:
            named = drug_name or drug_reference_id or ""
            raise ValidationError(
                f"“{named}” could not be matched to a known drug, so no safety check "
                "was run — this is not the same as “no interactions found”. Try the "
                "generic (INN) name, check the spelling of the brand name, or ask for the brand "
                "to be added to the drug vocabulary.",
                # ``detail`` is logged, correlated by request id to an access-log line whose
                # URL carries the patient id. The proposed drug name is that patient's
                # prescribing, so it stays out of the log; the reference id is a row in the
                # shared drug vocabulary and identifies the resolution failure on its own.
                detail=(
                    f"unresolved drug reference_id={drug_reference_id!r} "
                    f"(free-text name supplied: {bool(drug_name)})"
                ),
            )

        # The proposed drug's own reference id stays in current_meds if the patient is already
        # on it: check_duplicate_therapy needs to see it to detect "already an active order for
        # this exact product". check_interactions self-skips that pair, so nothing else in
        # evaluate_drug_safety is affected.
        proposed = _drug_ref(vocab)
        ctx = await self._build_context(
            patient_id, proposed_reference_ids=ingredient_reference_ids(proposed)
        )
        # Appended, not folded into ``evaluate_drug_safety``: each is a statement about the chart
        # rather than about the proposed drug, so it must not be repeated once per drug by the
        # callers that evaluate several against one context (``screen_text``, ``active_flags``).
        # This call evaluates exactly one, so appending them here shows them once — and this is
        # the only path the Safety screen calls, so a chart-level flag left out of it is a flag
        # no clinician sees.
        flags = (
            evaluate_drug_safety(proposed, ctx)
            + check_unevaluated_medications(ctx)
            + check_unevaluated_allergies(ctx)
            + check_unevaluated_conditions(ctx)
            + check_hepatic_severity(ctx)
        )
        check_ids = await self._persist(account_id, patient_id, vocab, flags)
        return vocab, ctx, flags, check_ids

    async def chart_completeness_flags(self, patient_id: uuid.UUID) -> list[SafetyFlag]:
        """The chart-level flags: what could not be read, and how impaired this liver is.

        Statements about the chart rather than about any one proposed drug — an unreadable
        medication line, an allergen the vocabulary cannot identify, a Child-Pugh window — so
        ``GET ../flags`` wants them once for the whole chart rather than repeated under every
        drug. Served from the same memoised patient facts the surrounding call already built, so
        they cost no additional query.

        The hepatic severity sits here rather than in ``evaluate_drug_safety`` for that reason
        and one more: it bears on every hepatically cleared drug, including the forty-odd in this
        vocabulary carrying no curated hepatic rule, so attaching it to a particular drug would
        misstate what it is about.
        """
        facts = await self._patient_facts(patient_id)
        return (
            check_unevaluated_medications(facts)
            + check_unevaluated_allergies(facts)
            + check_unevaluated_conditions(facts)
            + check_hepatic_severity(facts)
        )

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
            raise NotFoundError(
                "That safety check was not found on this patient's chart. Re-run the drug "
                "safety check to get a current one before recording an override.",
                detail=f"check {drug_safety_check_id} missing or on another patient",
            )
        if not check.is_hard_block:
            raise ValidationError(
                "This flag is advisory, not a hard block, so it needs no override — you can "
                "proceed and record your reasoning in the encounter note. Only hard blocks "
                "(documented allergy or contraindication) require a documented override.",
                detail=f"check {drug_safety_check_id} is not a hard block",
            )

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
            # Not the reasoning text. It is free text a clinician typed to justify prescribing
            # past a hard block, so it is the payload most likely to name someone — and
            # audit_logs.payload is unencrypted, immutable and never pruned, which makes a name
            # written here impossible to correct or erase on a DPDP request. It is stored on
            # drug_safety_overrides, an append-only table this row now points at by id, so the
            # trail still leads to the documented justification without copying it.
            payload={
                "check_type": check.check_type,
                "summary": check.summary,
                "override_id": str(override.id),
                "reasoning_chars": len(override.reasoning),
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

    async def screen_text(self, patient_id: uuid.UUID, text: str) -> list[SafetyFlag]:
        """Deterministic conflicts between the drugs *named in* ``text`` and this patient.

        The screen behind Critical Safety Rule #3 for output the pipeline generates rather than
        output a clinician proposes. A retrieved guideline is written for a population and knows
        nothing about the patient it is about to be shown beside: the shipped ICMR corpus says
        "Paracetamol is preferred for fever" in its dengue workflow and "Metformin is the
        preferred first-line pharmacotherapy" in its diabetes one, and both reach the clinician
        as cited management options. Until this existed, nothing compared either against the
        documented allergy or the measured eGFR in the same case state — so a patient allergic
        to paracetamol was shown a guideline-cited suggestion to give it, carrying every signal
        that it had been checked.

        Deliberately *not* :meth:`_reject_if_multiple_drugs`, which refuses a multi-drug string
        at the clinician-proposal boundary. There, naming two drugs makes the verdict ambiguous
        and one of them would be silently dropped. Here, naming several drugs is what a
        guideline sentence normally does, and every one of them is evaluated: refusing would
        turn "this option names two drugs" into no check at all, which is the failure that
        boundary exists to prevent, inverted.

        Whole-name matching only, via :meth:`DrugResolver.rows_named_in` — never the fuzzy
        tier. Fuzzy matching earns its place on a name a human typed or OCR lifted off a
        prescription; run over a paragraph of prose it would invent conflicts against drugs the
        text never mentioned, and a spurious hard block on a correct guideline recommendation
        teaches clinicians to click past hard blocks.

        Offline and deterministic, like every other check here: no LLM, and nothing but the
        patient's own rows and the curated rule tables.
        """
        named = await self.resolver.rows_named_in(text)
        if not named:
            return []
        # Every molecule of every named product, not just the products' own ids: a guideline
        # sentence naming a combination brand still has to load its ingredients' rules.
        ctx = await self._build_context(
            patient_id,
            proposed_reference_ids={
                ref for row in named.values() for ref in ingredient_reference_ids(_drug_ref(row))
            },
        )
        flags: list[SafetyFlag] = []
        # Sorted so a run's output does not depend on dictionary insertion order, which follows
        # where in the sentence each drug happened to appear.
        for reference_id in sorted(named):
            row = named[reference_id]
            flags.extend(evaluate_drug_safety(_drug_ref(row), ctx))
        return flags

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

        The vocabulary rows for all current medications are fetched in one batched query
        rather than one per drug, so the query count stays flat as the medication list grows.
        """
        await self._patient(account_id, patient_id)
        ctx = await self._build_context(patient_id)
        out: list[tuple[DrugVocabulary, list[SafetyFlag]]] = []
        seen_refs = {m.reference_id for m in ctx.current_meds}
        vocab_by_ref = await self.resolver.resolve_reference_ids(seen_refs)
        for ref in sorted(seen_refs):
            vocab = vocab_by_ref.get(ref)
            if vocab is None:
                continue
            sub_ctx = replace(
                ctx, current_meds=[m for m in ctx.current_meds if m.reference_id != ref]
            )
            flags = evaluate_drug_safety(_drug_ref(vocab), sub_ctx)
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
