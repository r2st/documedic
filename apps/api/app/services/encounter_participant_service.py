"""Sharing one consultation with a colleague, and the role that comes with it.

The access model in one paragraph: a chart belongs to one account, and that account is the only
one that may grant participation. A participant reaches the *encounter* — never the chart, never
its labs, never its other visits — and what they may do with it is decided by
``app.core.encounter_roles``, which is a frozen table rather than a query.

Two refusals here are worth reading before the code, because both look like restrictions and are
really about keeping the record true.

**A signed note cannot gain an author or a supervisor.** Those two roles assert that somebody
took part in the consultation as it happened. Adding one afterwards would rewrite who attended a
visit that has already been attested to — the same objection that freezes a signed encounter's
content (Critical Safety Rule #7 applied to attendance rather than to text). ``consulting`` is
deliberately still allowed: a second opinion sought a week later is an ordinary thing to record,
and it changes no attestation.

**A grant names a purpose and a removal names a reason.** Both are required columns. An access
grant to a clinical record that cannot say why it was made is the row nobody can justify at a
review, and a withdrawal with no reason is reliably the half of the review somebody needs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encounter_roles import ROLES_FROZEN_BY_SIGNATURE, may_sign
from app.exceptions import (
    EncounterNotFoundError,
    ParticipantAccountNotFoundError,
    ParticipantAlreadyPresentError,
    ParticipantNotFoundError,
    ParticipantRoleNotPermittedError,
)
from app.models.encounter import SIGNED_STATUSES, Encounter
from app.models.encounter_participant import EncounterParticipant
from app.models.patient import Patient
from app.models.user import Account
from app.services.audit_service import AuditService

# The most encounters the "shared with me" list returns in one page.
MAX_SHARED_ROWS = 200


class EncounterParticipantService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    # --- Granting and withdrawing --------------------------------------------------------

    async def _owned_encounter(
        self, account_id: uuid.UUID, patient_id: uuid.UUID, encounter_id: uuid.UUID
    ) -> Encounter:
        """The encounter, checked to be on a chart ``account_id`` owns.

        Ownership is re-checked here rather than trusted from the router, and the encounter is
        scoped by ``patient_id`` as well as by its own id. Both for the reason
        ``EncounterService.get`` gives: an encounter id from another chart must be a 404 on this
        route before any other check can be consulted, because this is the one route in the API
        that *hands out access*, and a cross-chart reference here would be a grant over somebody
        else's record.
        """
        from app.services.patient_service import PatientService

        await PatientService(self.db).get(account_id, patient_id)
        encounter = (
            await self.db.execute(
                select(Encounter).where(
                    Encounter.id == encounter_id,
                    Encounter.patient_id == patient_id,
                    Encounter.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if encounter is None:
            raise EncounterNotFoundError()
        return encounter

    async def add(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        encounter_id: uuid.UUID,
        email: str,
        role: str,
        purpose: str,
    ) -> tuple[EncounterParticipant, Account]:
        """Grant ``email``'s account participation in this encounter. Audited.

        The colleague is named by email rather than by account id, because an account id is not
        something one clinician knows about another and a UI that asked for one would be a UI
        nobody could use. The cost is an existence oracle — an authenticated caller learns
        whether an address has an account here — and it is accepted deliberately, unlike on the
        password-reset routes where it is not: those are anonymous and their whole threat model
        is an attacker enumerating addresses, while this one is a signed-in clinician naming a
        specific colleague, and answering "shared" when nothing was shared is a failure mode
        that ends with the record being emailed instead. The route carries a rate limit.

        Refuses a clinical role on a signed note, and a second live grant to the same account.
        """
        encounter = await self._owned_encounter(account_id, patient_id, encounter_id)
        if encounter.status in SIGNED_STATUSES and role in ROLES_FROZEN_BY_SIGNATURE:
            raise ParticipantRoleNotPermittedError(
                detail=(
                    f"role {role!r} refused on {encounter.status} encounter {encounter_id}: "
                    "it would rewrite who attended an attested visit"
                )
            )

        # Lowercased because that is the invariant the accounts table is held to everywhere
        # else in this codebase — see AuthService — and a lookup that did not match it would
        # report "no such colleague" for an address that differs only in case.
        normalized = email.strip().lower()
        colleague = (
            await self.db.execute(
                select(Account).where(Account.email == normalized, Account.is_deleted.is_(False))
            )
        ).scalar_one_or_none()
        if colleague is None:
            raise ParticipantAccountNotFoundError(detail=f"no live account for {normalized!r}")

        existing = await self._live_participation(encounter_id, colleague.id)
        if existing is not None:
            raise ParticipantAlreadyPresentError(
                detail=(
                    f"account {colleague.id} already participates in {encounter_id} "
                    f"as {existing.role!r}"
                )
            )

        participant = EncounterParticipant(
            encounter_id=encounter_id,
            account_id=colleague.id,
            role=role,
            granted_by_account_id=account_id,
            purpose=purpose,
        )
        self.db.add(participant)
        await self.db.flush()
        await self.audit.record(
            action="encounter_participant_added",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="encounter_participant",
            entity_id=participant.id,
            # The role and the granted account's id, never the email and never the purpose
            # text. ``audit_logs.payload`` is unencrypted and never pruned; the id identifies
            # the colleague for a review without writing an address into it, and the purpose is
            # free text a clinician typed about a patient.
            payload={
                "role": role,
                "encounter_id": str(encounter_id),
                "participant_account_id": str(colleague.id),
            },
        )
        return participant, colleague

    async def remove(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        encounter_id: uuid.UUID,
        participant_id: uuid.UUID,
        reason: str,
    ) -> EncounterParticipant:
        """Withdraw a participation. Sets the tombstone; never deletes the row. Audited.

        Idempotence is deliberately *not* offered: withdrawing an already-withdrawn access
        raises :class:`ParticipantNotFoundError` rather than succeeding quietly, because the two
        outcomes a caller cares about — "I have just revoked this" and "this was revoked in
        March by somebody else" — must not render as the same 200.
        """
        await self._owned_encounter(account_id, patient_id, encounter_id)
        participant = (
            await self.db.execute(
                select(EncounterParticipant).where(
                    EncounterParticipant.id == participant_id,
                    EncounterParticipant.encounter_id == encounter_id,
                    EncounterParticipant.removed_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if participant is None:
            raise ParticipantNotFoundError()

        participant.removed_at = datetime.now(UTC)
        participant.removal_reason = reason
        await self.db.flush()
        await self.audit.record(
            action="encounter_participant_removed",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="encounter_participant",
            entity_id=participant.id,
            payload={
                "role": participant.role,
                "encounter_id": str(encounter_id),
                "participant_account_id": str(participant.account_id),
            },
        )
        return participant

    async def list_for_encounter(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        encounter_id: uuid.UUID,
        include_removed: bool = False,
    ) -> list[tuple[EncounterParticipant, Account]]:
        """Who participates in this encounter, and — optionally — who used to.

        Ordered oldest first, which reads as the sequence of the consultation: the author was
        added before the supervisor was asked, and the consultant after both.
        """
        await self._owned_encounter(account_id, patient_id, encounter_id)
        statement = (
            select(EncounterParticipant, Account)
            .join(Account, Account.id == EncounterParticipant.account_id)
            .where(EncounterParticipant.encounter_id == encounter_id)
        )
        if not include_removed:
            statement = statement.where(EncounterParticipant.removed_at.is_(None))
        statement = statement.order_by(
            EncounterParticipant.created_at.asc(), EncounterParticipant.id.asc()
        )
        rows = (await self.db.execute(statement)).tuples().all()
        await self.audit.record(
            action="encounter_participants_viewed",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="encounter",
            entity_id=encounter_id,
            payload={"participants": len(rows), "include_removed": include_removed},
        )
        return [(participant, account) for participant, account in rows]

    # --- The participant's own view -------------------------------------------------------

    async def _live_participation(
        self, encounter_id: uuid.UUID, account_id: uuid.UUID
    ) -> EncounterParticipant | None:
        return (
            await self.db.execute(
                select(EncounterParticipant).where(
                    EncounterParticipant.encounter_id == encounter_id,
                    EncounterParticipant.account_id == account_id,
                    EncounterParticipant.removed_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    async def shared_encounter(
        self, *, account_id: uuid.UUID, encounter_id: uuid.UUID
    ) -> tuple[Encounter, Patient, EncounterParticipant]:
        """One encounter shared with ``account_id``, its chart, and the participation behind it.

        Every failure is the same 404. A withdrawn participation, an encounter on a chart that
        has since been withdrawn, an id that never existed and an id belonging to somebody
        else's colleague must not be distinguishable: this route's caller is by construction
        somebody outside the owning practice, and a 403 here would confirm that a given
        encounter id is real.

        The chart's own soft-delete is honoured. A patient who withdraws consent withdraws it
        from the colleague who was shown one visit as much as from the practice, and a share is
        not a copy that outlives the record.
        """
        participation = await self._live_participation(encounter_id, account_id)
        if participation is None:
            raise EncounterNotFoundError()
        row = (
            (
                await self.db.execute(
                    select(Encounter, Patient)
                    .join(Patient, Patient.id == Encounter.patient_id)
                    .where(
                        Encounter.id == encounter_id,
                        Encounter.is_deleted.is_(False),
                        Patient.is_deleted.is_(False),
                    )
                )
            )
            .tuples()
            .one_or_none()
        )
        if row is None:
            raise EncounterNotFoundError()
        encounter, patient = row
        return encounter, patient, participation

    async def list_shared_with(
        self, *, account_id: uuid.UUID, limit: int = 50
    ) -> list[tuple[Encounter, Patient, EncounterParticipant]]:
        """Encounters shared with this account, most recently granted first.

        This is the participant's only way in. They cannot list a chart they do not own, so
        without it a share would be a link somebody had to be sent out of band — which is the
        thing this feature exists to stop.
        """
        rows = (
            (
                await self.db.execute(
                    select(Encounter, Patient, EncounterParticipant)
                    .join(
                        EncounterParticipant,
                        EncounterParticipant.encounter_id == Encounter.id,
                    )
                    .join(Patient, Patient.id == Encounter.patient_id)
                    .where(
                        EncounterParticipant.account_id == account_id,
                        EncounterParticipant.removed_at.is_(None),
                        Encounter.is_deleted.is_(False),
                        Patient.is_deleted.is_(False),
                    )
                    .order_by(
                        EncounterParticipant.created_at.desc(),
                        EncounterParticipant.id.desc(),
                    )
                    .limit(min(limit, MAX_SHARED_ROWS))
                )
            )
            .tuples()
            .all()
        )
        await self.audit.record(
            action="shared_encounters_viewed",
            account_id=account_id,
            patient_id=None,
            entity_type="encounter",
            entity_id=None,
            payload={"shared": len(rows)},
        )
        return [(encounter, patient, participant) for encounter, patient, participant in rows]

    async def assert_may_sign(
        self, *, account_id: uuid.UUID, encounter_id: uuid.UUID
    ) -> tuple[Encounter, Patient, EncounterParticipant]:
        """The encounter, if ``account_id`` participates in a role that may attest to it.

        A role that may read but not sign is a **403**, not a 404: the caller has been shown
        this encounter, so there is nothing left to conceal, and "you may look at this but not
        sign it" is exactly what they need to be told. Contrast ``shared_encounter``, where the
        caller has been shown nothing and every failure must look alike.
        """
        encounter, patient, participation = await self.shared_encounter(
            account_id=account_id, encounter_id=encounter_id
        )
        if not may_sign(participation.role):
            raise ParticipantRoleNotPermittedError(
                detail=(
                    f"role {participation.role!r} may read encounter {encounter_id} "
                    "but may not attest to it"
                )
            )
        return encounter, patient, participation
