"""Encounter lifecycle: open a visit, edit it, sign it, amend it.

The rule this module exists to hold is that **a signature is final**. Up to it, a visit is a
working draft and edits leave no trace beyond ``updated_at``; after it, the encounter's clinical
content can never change, and a correction is a new encounter that names the one it supersedes
and says why. That is the same shape ``ClinicalSuggestion`` already has for the engine's output
(Critical Safety Rule #7), applied to the note a clinician writes by hand.

Three layers hold it, deliberately overlapping:

* this service, which refuses the transition and explains what to do instead;
* ``ck_encounters_*`` and ``uq_encounters_one_signed_amendment`` on the table, which hold
  whatever writes to it;
* ``trg_encounters_signed_frozen`` (migration 0031, PostgreSQL), which refuses an UPDATE that
  changes a frozen column on a signed row even if it comes from a psql prompt.

Authorization is the account-ownership check every patient-scoped route performs, via
:class:`~app.services.patient_service.PatientService`. There is no role model in this product —
one account is one clinician — so "who may sign" is "the clinician whose chart this is", and
*which* clinician signed is recorded on the row rather than inferred from the chart's owner.
Writes go through ``get_for_processing`` (consent gate) and reads through ``get``: an encounter
is new personal data being recorded, and a chart whose consent has been withdrawn must stop
accepting it while staying readable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    EncounterAlreadyAmendedError,
    EncounterNotFoundError,
    EncounterSignedError,
    EncounterTransitionError,
    ValidationError,
)
from app.models.encounter import SIGNED_STATUSES, Encounter
from app.schemas.encounter import EncounterAmend, EncounterCreate, EncounterUpdate
from app.services.audit_service import AuditService

# What a *sign* may be applied to. Not ``signed`` — a second signature would either overwrite
# the first attestation or record two, with nothing to say which one the chart means. The
# editable set is the complement of ``SIGNED_STATUSES`` and is tested against that directly, so
# a fifth status added later cannot become silently editable by being left off a list here.
_SIGNABLE_STATUSES = ("draft", "in_progress")

# PostgreSQL names the index it rejected; SQLite names the columns. Both spellings are pinned by
# test against the live drivers, for the same reason ``_OBSERVATION_CONFLICT_MARKERS`` in
# document_service is: a wording change must fail a test rather than turn a lost amendment race
# into a 500 the clinician cannot act on.
_AMENDMENT_CONFLICT_MARKERS = (
    "uq_encounters_one_signed_amendment",
    "encounters.amends_encounter_id",
)


def _is_amendment_conflict(exc: IntegrityError) -> bool:
    """Whether this integrity error is the one-signed-amendment constraint firing.

    Narrow on purpose: any other constraint violated while signing is a bug, and must keep its
    traceback rather than being reported to the clinician as somebody else getting there first.
    """
    message = str(exc.orig)
    return any(marker in message for marker in _AMENDMENT_CONFLICT_MARKERS)


class EncounterService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def list(
        self, patient_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[Encounter], int]:
        """One page of the chart's visits, newest first, with the count before paging.

        Ordered by ``encounter_date`` and then by ``id``. The tie-break is not decoration: the
        ordering column is a ``Date``, so a chart with two visits on one day has ties by
        construction, and LIMIT/OFFSET over a partial order is a lottery rather than a partition
        — page 2 can repeat a row page 1 already showed and silently skip another. Same defect,
        and same fix, as the patient list (see ``PatientService.list``).
        """
        base = (
            select(Encounter)
            .where(Encounter.patient_id == patient_id, Encounter.is_deleted.is_(False))
            .order_by(Encounter.encounter_date.desc(), Encounter.id)
        )
        total = await self.db.scalar(
            select(func.count())
            .select_from(Encounter)
            .where(Encounter.patient_id == patient_id, Encounter.is_deleted.is_(False))
        )
        rows = await self.db.execute(base.limit(limit).offset(offset))
        return list(rows.scalars().all()), int(total or 0)

    async def get(
        self, patient_id: uuid.UUID, encounter_id: uuid.UUID, *, for_update: bool = False
    ) -> Encounter:
        """One visit on this chart, or :class:`EncounterNotFoundError`.

        Scoped to ``patient_id`` as well as to the encounter's own id, so an encounter id from
        another clinician's chart is a 404 on this route even before the ownership check the
        caller has already performed can be consulted. Cross-chart references are the failure
        this shape prevents; see ``tests/test_body_scoped_patient_references.py`` for the class
        of bug (a safety report written into another chart's audit trail).

        ``for_update`` locks the row, and only the amendment path asks for it: signing an
        amendment reads the original's status and then changes it, and two amendments of one
        visit racing through that window is exactly what the sign path must not allow. On SQLite
        the lock is silently dropped — ``uq_encounters_one_signed_amendment`` is what holds
        there.
        """
        statement = select(Encounter).where(
            Encounter.id == encounter_id,
            Encounter.patient_id == patient_id,
            Encounter.is_deleted.is_(False),
        )
        if for_update:
            statement = statement.with_for_update()
        encounter = (await self.db.execute(statement)).scalar_one_or_none()
        if encounter is None:
            raise EncounterNotFoundError()
        return encounter

    async def create(
        self, account_id: uuid.UUID, patient_id: uuid.UUID, data: EncounterCreate
    ) -> Encounter:
        """Open a visit as a ``draft``. Audited as ``encounter_created``."""
        _validate_telehealth(data.encounter_type, data.telehealth_modality)
        encounter = Encounter(
            patient_id=patient_id,
            encounter_date=data.encounter_date,
            encounter_type=data.encounter_type,
            presenting_complaint=data.presenting_complaint,
            clinician_notes=data.clinician_notes,
            telehealth_modality=data.telehealth_modality,
            billing_code=data.billing_code,
            status="draft",
        )
        self.db.add(encounter)
        await self.db.flush()
        await self.audit.record(
            action="encounter_created",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="encounter",
            entity_id=encounter.id,
            # Counts and identifiers only. ``audit_logs.payload`` is unencrypted, immutable and
            # never pruned, so the complaint and the notes must never land here — see
            # AUDIT_ACTIONS in models/audit_log.py.
            payload={"status": "draft"},
        )
        await self.db.commit()
        await self.db.refresh(encounter)
        return encounter

    async def update(
        self,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        encounter_id: uuid.UUID,
        data: EncounterUpdate,
    ) -> Encounter:
        """Edit an unsigned visit. Audited as ``encounter_updated``.

        Refuses a signed or amended encounter with :class:`EncounterSignedError`, which names
        the amendment path — this is the error a clinician is most likely to meet, and "you
        cannot do that" without "here is what to do instead" is how a correction ends up being
        typed into the next visit's notes.
        """
        encounter = await self.get(patient_id, encounter_id)
        if encounter.status in SIGNED_STATUSES:
            raise EncounterSignedError(
                detail=f"edit refused: encounter {encounter_id} is {encounter.status}"
            )

        fields = data.model_dump(exclude_unset=True)
        # Validated against the row as it *will be*, not against what the request happens to
        # carry. A PATCH that changes the type from teleconsultation to outpatient leaves the
        # modality behind, and one that sets a modality on an encounter whose type is already
        # outpatient names neither field the check needs — both would slip past a validation
        # that only looked at the payload, and both land as a database constraint violation
        # (a 500) rather than as the 422 the clinician can act on.
        _validate_telehealth(
            fields.get("encounter_type", encounter.encounter_type),
            fields.get("telehealth_modality", encounter.telehealth_modality),
        )
        for key, value in fields.items():
            setattr(encounter, key, value)
        await self.db.flush()
        await self.audit.record(
            action="encounter_updated",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="encounter",
            entity_id=encounter.id,
            # The field *names* that changed, never their values: which parts of a note were
            # revised is an accountability fact, what they now say is clinical content.
            payload={"status": encounter.status, "fields": sorted(fields)},
        )
        await self.db.commit()
        await self.db.refresh(encounter)
        return encounter

    async def sign(
        self, account_id: uuid.UUID, patient_id: uuid.UUID, encounter_id: uuid.UUID
    ) -> Encounter:
        """Attest to a visit, freezing its content. Audited as ``encounter_signed``.

        When the encounter being signed is itself an amendment, the encounter it amends moves to
        ``amended`` in the same transaction — that, and not the drafting of the amendment, is
        the moment the correction becomes the current version of the visit. An abandoned
        amendment draft therefore leaves the original exactly as it was.
        """
        encounter = await self.get(patient_id, encounter_id)
        if encounter.status not in _SIGNABLE_STATUSES:
            raise EncounterTransitionError(
                detail=f"sign refused: encounter {encounter_id} is already {encounter.status}"
            )

        superseded: Encounter | None = None
        if encounter.amends_encounter_id is not None:
            # Locked, then re-checked: the status read here decides whether this amendment may
            # land, and the row is about to be written on the strength of it.
            superseded = await self.get(patient_id, encounter.amends_encounter_id, for_update=True)
            if superseded.status != "signed":
                raise EncounterAlreadyAmendedError(
                    detail=(
                        f"amendment refused: target {superseded.id} is {superseded.status}, "
                        "expected signed"
                    )
                )

        now = datetime.now(UTC)
        encounter.status = "signed"
        encounter.signed_at = now
        encounter.signed_by_account_id = account_id
        if superseded is not None:
            superseded.status = "amended"
            superseded.amended_at = now

        await self.audit.record(
            action="encounter_signed",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="encounter",
            entity_id=encounter.id,
            payload={
                "status": "signed",
                "amends_encounter_id": (
                    str(encounter.amends_encounter_id) if encounter.amends_encounter_id else None
                ),
            },
        )
        if superseded is not None:
            await self.audit.record(
                action="encounter_amended",
                account_id=account_id,
                patient_id=patient_id,
                entity_type="encounter",
                entity_id=superseded.id,
                payload={"status": "amended", "amended_by_encounter_id": str(encounter.id)},
            )
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            if _is_amendment_conflict(exc):
                raise EncounterAlreadyAmendedError(detail=str(exc.orig)) from exc
            raise
        await self.db.refresh(encounter)
        return encounter

    async def amend(
        self,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        encounter_id: uuid.UUID,
        data: EncounterAmend,
    ) -> Encounter:
        """Open an amendment to a signed visit, as a new draft. Audited as
        ``encounter_amendment_opened``.

        The amendment starts life unsigned on purpose: a correction to an attested note is
        itself a clinical statement, and it becomes the current version of the visit only when
        the clinician signs it. Until then the signed original is what the chart shows.

        Fields the body leaves out are copied from the original, so an amendment that fixes only
        the note does not have to restate the date — and so the amendment reads as a complete
        version of the visit rather than a diff a later reader has to reassemble.
        """
        original = await self.get(patient_id, encounter_id)
        if original.status == "amended":
            raise EncounterAlreadyAmendedError(
                detail=f"amend refused: encounter {encounter_id} already has a signed amendment"
            )
        if original.status != "signed":
            raise EncounterTransitionError(
                detail=(
                    f"amend refused: encounter {encounter_id} is {original.status}; only a "
                    "signed encounter can be amended"
                )
            )

        supplied = data.model_dump(exclude_unset=True)
        encounter_type = _pick(supplied, "encounter_type", original.encounter_type)
        telehealth_modality = _pick(supplied, "telehealth_modality", original.telehealth_modality)
        # Checked against the amendment's own resulting pair, not the original's. An amendment
        # that corrects the type from teleconsultation to outpatient inherits the modality from
        # the row it is amending, and would otherwise be written as an in-clinic visit still
        # claiming to have been conducted over video.
        _validate_telehealth(encounter_type, telehealth_modality)
        amendment = Encounter(
            patient_id=patient_id,
            # Carried across so the amendment cites the same scan the original was read from.
            # It is the same visit; only what was written about it has changed.
            source_document_id=original.source_document_id,
            encounter_date=_pick(supplied, "encounter_date", original.encounter_date),
            encounter_type=encounter_type,
            presenting_complaint=_pick(
                supplied, "presenting_complaint", original.presenting_complaint
            ),
            clinician_notes=_pick(supplied, "clinician_notes", original.clinician_notes),
            telehealth_modality=telehealth_modality,
            billing_code=_pick(supplied, "billing_code", original.billing_code),
            status="draft",
            amends_encounter_id=original.id,
            amendment_reason=data.amendment_reason,
        )
        self.db.add(amendment)
        await self.db.flush()
        await self.audit.record(
            action="encounter_amendment_opened",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="encounter",
            entity_id=amendment.id,
            # The reason is deliberately absent: it is clinician-written prose about the
            # patient's record, and this payload is unencrypted and never pruned. It lives on
            # the encounter row, which is where a reader of the chart finds it.
            payload={"status": "draft", "amends_encounter_id": str(original.id)},
        )
        await self.db.commit()
        await self.db.refresh(amendment)
        return amendment


def _pick[T](supplied: dict, key: str, fallback: T) -> T:
    """The submitted value for ``key`` if the body carried one, else the original's.

    Generic in the fallback's type so each call site keeps the column's own type — the amendment
    passes the results straight into ``Encounter(...)`` and into ``_validate_telehealth``, and a
    union of every column's type would make all of those ``date | str | None``.

    Reads ``exclude_unset`` output rather than testing for ``None``, because the two are
    different requests: a body that omits ``clinician_notes`` keeps the original note, and one
    that sends ``null`` is a clinician deleting it. Collapsing them would make the second
    impossible to express.
    """
    return supplied[key] if key in supplied else fallback


def _validate_telehealth(encounter_type: str | None, modality: str | None) -> None:
    """A modality belongs to a teleconsultation, and a teleconsultation needs one.

    Both directions, and each closes a different hole:

    * **A teleconsultation without a modality** is a visit the telemedicine prescribing rules
      cannot be applied to at all (``app.core.telehealth`` returns nothing when the modality is
      unknown, deliberately — inventing a restriction from silence would put format-dependent
      flags on every in-clinic prescription in the system). Before these columns existed that
      was the state of every remote visit; allowing it to persist would mean the encounter now
      *claims* to have been checked while nothing checked it.
    * **A modality on an in-clinic visit** is a contradiction rather than extra information, and
      one that would make the encounter's own record of itself unreadable: a row saying
      "outpatient, conducted over video" gives a later reader no way to know which half is
      wrong.

    Raised here as a 422 as well as being enforced by ``ck_encounters_telehealth_modality_``
    ``matches_type`` on the table. Without this the constraint still holds the invariant — but
    it holds it by aborting the transaction, which reaches the clinician as a 500 with no
    indication of which field to fix.
    """
    is_tele = encounter_type == "teleconsultation"
    if is_tele and not modality:
        raise ValidationError(
            "A teleconsultation needs to say how it was conducted: set telehealth_modality to "
            "'video' or 'audio'. The distinction decides which prescribing rules apply — on a "
            "video call the patient has been seen and on a telephone call they have not.",
            detail="teleconsultation without telehealth_modality",
        )
    if modality and not is_tele:
        raise ValidationError(
            "telehealth_modality applies only to an encounter of type 'teleconsultation'. "
            f"This one is {encounter_type or 'untyped'}.",
            detail=f"telehealth_modality set on encounter_type={encounter_type!r}",
        )
