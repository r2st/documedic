"""Issuing, revoking and authenticating patient-portal credentials, and the reads behind them.

The access model is one sentence: a credential names one chart, is revocable, expires, and can
only read. Everything else in this module is that sentence enforced in the places it could go
wrong.

Three of those places are worth naming up front.

**Authentication cannot fall through to the clinician's.** The token is an opaque random string,
never a JWT, so ``app.core.security.decode_token`` cannot parse it and
``app.dependencies.get_current_account`` cannot accept it. The two credential vocabularies do
not overlap at all, which is a stronger guarantee than any amount of careful checking of a
``type`` claim would be — and it holds against future edits to either path.

**A withdrawn chart closes the portal.** The grant is joined to a live patient on every read.
Consent withdrawal, chart deletion and revocation all produce the same 401; a credential is not
a copy that outlives the record it points at.

**The reads are narrow by construction.** Nothing here selects a condition, a clinical
suggestion or a clinician's note. That is not a filter that could be edited wrong later — those
tables are not queried.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.portal_redaction import (
    PortalLabDecision,
    PortalLabInput,
    PortalMedication,
    PortalMedicationInput,
    decide_lab_release,
    grant_is_live,
    present_medication,
)
from app.exceptions import PortalAccessError, PortalGrantNotFoundError, ValidationError
from app.models.appointment import Appointment
from app.models.critical_lab_acknowledgement import CriticalLabAcknowledgement
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.patient_portal_grant import PatientPortalGrant
from app.services.audit_service import AuditService

# The longest a portal credential may live. Ninety days is an episode of care, not a
# relationship: a link that outlives the reason it was issued is a link nobody remembers
# granting, on a device nobody remembers using.
MAX_GRANT_DAYS = 90
DEFAULT_GRANT_DAYS = 30

# Bounds on what one portal read returns. A patient's own record is not a bulk-export surface,
# and these are the ceilings that keep an issued credential from being one.
MAX_PORTAL_LABS = 200
MAX_PORTAL_MEDICATIONS = 100
MAX_PORTAL_APPOINTMENTS = 50

# Bytes of entropy in the token. 32 bytes is 256 bits; the value is URL-safe base64 because the
# realistic delivery is a link or a copied string typed on a phone.
_TOKEN_BYTES = 32


def hash_portal_token(token: str) -> str:
    """SHA-256 hex of a portal token. Raw tokens are never persisted.

    Its own function rather than reusing ``app.core.security.hash_token`` — same algorithm,
    different credential vocabulary. Sharing the helper would be one edit away from sharing a
    lookup, and the two must never resolve each other's tokens.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedGrant:
    """A newly issued credential. ``token`` exists only in this object, once."""

    grant: PatientPortalGrant
    token: str


@dataclass(frozen=True)
class PortalContext:
    """The authenticated patient behind a portal request."""

    patient: Patient
    grant: PatientPortalGrant


class PatientPortalService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    # --- The clinician's side --------------------------------------------------------------

    async def issue(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        days_valid: int = DEFAULT_GRANT_DAYS,
        label: str | None = None,
    ) -> IssuedGrant:
        """Mint a credential for this chart. The token is returned once and never stored.

        Goes through ``get_for_processing`` rather than ``get``: issuing portal access to a
        chart whose patient has withdrawn consent would be the clearest possible violation of
        the withdrawal, and it is the consent gate that says so.
        """
        from app.services.patient_service import PatientService

        patient = await PatientService(self.db).get_for_processing(account_id, patient_id)
        if not 1 <= days_valid <= MAX_GRANT_DAYS:
            raise ValidationError(
                f"days_valid must be between 1 and {MAX_GRANT_DAYS}",
                detail=f"days_valid={days_valid}",
            )

        token = secrets.token_urlsafe(_TOKEN_BYTES)
        grant = PatientPortalGrant(
            patient_id=patient.id,
            issued_by_account_id=account_id,
            token_hash=hash_portal_token(token),
            label=label,
            expires_at=datetime.now(UTC) + timedelta(days=days_valid),
        )
        self.db.add(grant)
        await self.db.flush()
        await self.audit.record(
            action="portal_access_granted",
            account_id=account_id,
            patient_id=patient.id,
            entity_type="patient_portal_grant",
            entity_id=grant.id,
            # No token, no hash, no label. The token would be a working credential written into
            # an unencrypted, never-pruned table; the hash would be enough to recognise one; the
            # label is free text a clinician typed about a patient.
            payload={"days_valid": days_valid, "labelled": label is not None},
        )
        return IssuedGrant(grant=grant, token=token)

    async def revoke(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, grant_id: uuid.UUID, reason: str
    ) -> PatientPortalGrant:
        """Withdraw a credential. Effective on the next request; the row is kept.

        Revoking an already-revoked grant raises rather than succeeding quietly, for the reason
        the participant withdrawal gives: "I have just stopped this" and "this stopped in March"
        are answers a practice acts on differently.
        """
        from app.services.patient_service import PatientService

        await PatientService(self.db).get(account_id, patient_id)
        grant = (
            await self.db.execute(
                select(PatientPortalGrant).where(
                    PatientPortalGrant.id == grant_id,
                    PatientPortalGrant.patient_id == patient_id,
                    PatientPortalGrant.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            raise PortalGrantNotFoundError()

        grant.revoked_at = datetime.now(UTC)
        grant.revocation_reason = reason
        await self.db.flush()
        await self.audit.record(
            action="portal_access_revoked",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="patient_portal_grant",
            entity_id=grant.id,
            payload={"grant_id": str(grant.id)},
        )
        return grant

    async def list_grants(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID
    ) -> list[PatientPortalGrant]:
        """Every credential ever issued for this chart, newest first, live and withdrawn alike.

        Withdrawn ones are included and not filtered out: "who has had access to this record,
        and between when" is the question, and a list of only the live grants answers a
        different and less useful one.
        """
        from app.services.patient_service import PatientService

        await PatientService(self.db).get(account_id, patient_id)
        rows = (
            (
                await self.db.execute(
                    select(PatientPortalGrant)
                    .where(PatientPortalGrant.patient_id == patient_id)
                    .order_by(PatientPortalGrant.created_at.desc(), PatientPortalGrant.id.desc())
                )
            )
            .scalars()
            .all()
        )
        await self.audit.record(
            action="portal_access_listed",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="patient",
            entity_id=patient_id,
            payload={"grants": len(rows)},
        )
        return list(rows)

    # --- The patient's side ----------------------------------------------------------------

    async def authenticate(self, token: str) -> PortalContext:
        """Resolve a portal token to its chart, or raise :class:`PortalAccessError`.

        Every failure is the same 401 with the same message: an expired credential, a revoked
        one, one for a chart that has been withdrawn, and a string that was never a token. The
        caller is an unauthenticated member of the public holding a link, and distinguishing
        those would tell somebody who found a link on a shared phone which kind of thing they
        had found.

        The lookup is by hash, so a token is never compared as text and never appears in a
        query the database might log.
        """
        digest = hash_portal_token(token)
        row = (
            (
                await self.db.execute(
                    select(PatientPortalGrant, Patient)
                    .join(Patient, Patient.id == PatientPortalGrant.patient_id)
                    .where(
                        PatientPortalGrant.token_hash == digest,
                        Patient.is_deleted.is_(False),
                    )
                )
            )
            .tuples()
            .one_or_none()
        )
        if row is None:
            raise PortalAccessError(detail="portal token does not match a live grant")
        grant, patient = row
        if not grant_is_live(
            expires_at=grant.expires_at,
            revoked_at=grant.revoked_at,
            now=datetime.now(UTC),
        ):
            raise PortalAccessError(detail=f"portal grant {grant.id} is expired or revoked")
        grant.last_used_at = datetime.now(UTC)
        return PortalContext(patient=patient, grant=grant)

    async def record_read(self, context: PortalContext, *, section: str, items: int) -> None:
        """Audit one portal read.

        Attributed to the account that issued the credential, because the patient has no account
        id to attribute it to and an entry with no actor is an entry a review cannot follow.
        ``by_patient`` is what distinguishes it from that clinician's own reads of the same
        chart — without it, the trail would say the practice read the record when the practice
        was asleep.
        """
        await self.audit.record(
            action="portal_record_viewed",
            account_id=context.grant.issued_by_account_id,
            patient_id=context.patient.id,
            entity_type="patient_portal_grant",
            entity_id=context.grant.id,
            payload={"section": section, "items": items, "by_patient": True},
        )

    async def labs_for(self, context: PortalContext) -> list[PortalLabDecision]:
        """The patient's own results, newest first, with critical values embargoed.

        The embargo is the point of this method; ``app.core.portal_redaction`` holds the rule
        and this half supplies the one fact it cannot compute — whether a clinician has
        acknowledged the result.

        The acknowledgement lookup is scoped to the *issuing account*, matching
        ``LabSafetyService._acknowledged_lab_ids``. That is the conservative direction here: if
        a result were somehow acknowledged by an unrelated practice, this treats it as
        unacknowledged and withholds it, which errs towards the phone call.
        """
        rows = (
            (
                await self.db.execute(
                    select(LabResult)
                    .where(
                        LabResult.patient_id == context.patient.id,
                        LabResult.is_deleted.is_(False),
                    )
                    .order_by(LabResult.sample_date.desc().nullslast(), LabResult.id.desc())
                    .limit(MAX_PORTAL_LABS)
                )
            )
            .scalars()
            .all()
        )
        acknowledged = await self._acknowledged_ids(
            account_id=context.grant.issued_by_account_id,
            lab_ids=[row.id for row in rows],
        )
        return [
            decide_lab_release(
                PortalLabInput(
                    lab_result_id=str(row.id),
                    marker_name=row.marker_name,
                    value_numeric=(None if row.value_numeric is None else float(row.value_numeric)),
                    value_text=row.value_text,
                    unit=row.unit,
                    reference_low=(
                        None if row.reference_range_low is None else float(row.reference_range_low)
                    ),
                    reference_high=(
                        None
                        if row.reference_range_high is None
                        else float(row.reference_range_high)
                    ),
                    sample_date=None if row.sample_date is None else row.sample_date.date(),
                    is_abnormal=row.is_abnormal,
                    acknowledged=row.id in acknowledged,
                )
            )
            for row in rows
        ]

    async def _acknowledged_ids(
        self, *, account_id: uuid.UUID, lab_ids: list[uuid.UUID]
    ) -> set[uuid.UUID]:
        if not lab_ids:
            return set()
        rows = await self.db.execute(
            select(CriticalLabAcknowledgement.lab_result_id).where(
                CriticalLabAcknowledgement.account_id == account_id,
                CriticalLabAcknowledgement.lab_result_id.in_(lab_ids),
            )
        )
        return set(rows.scalars().all())

    async def medications_for(self, context: PortalContext) -> list[PortalMedication]:
        """The patient's current medicines, named the way they will recognise them.

        Current only. A patient's list of what to take today is the useful artefact; a full
        prescribing history read without a clinician beside it is a list of things they might
        conclude they should restart.
        """
        rows = (
            (
                await self.db.execute(
                    select(MedicationEvent)
                    .where(
                        MedicationEvent.patient_id == context.patient.id,
                        MedicationEvent.is_deleted.is_(False),
                        MedicationEvent.is_current.is_(True),
                    )
                    .order_by(
                        MedicationEvent.event_date.desc().nullslast(),
                        MedicationEvent.id.desc(),
                    )
                    .limit(MAX_PORTAL_MEDICATIONS)
                )
            )
            .scalars()
            .all()
        )
        return [
            present_medication(
                PortalMedicationInput(
                    medication_event_id=str(row.id),
                    display_name=row.brand_name_raw,
                    generic_name=row.generic_name,
                    dose=" ".join(part for part in (row.dose, row.dose_unit) if part) or None,
                    frequency=row.frequency,
                    route=row.route,
                    status=row.event_type,
                    started_on=row.event_date,
                    stopped_on=row.end_date,
                )
            )
            for row in rows
        ]

    async def appointments_for(self, context: PortalContext) -> list[Appointment]:
        """The patient's upcoming appointments, soonest first.

        Upcoming only, and cancelled ones excluded. A patient's portal answers "when am I next
        seen"; the diary's full history, including the visits they missed, is the practice's
        working record and reads very differently on a patient's phone.
        """
        rows = (
            (
                await self.db.execute(
                    select(Appointment)
                    .where(
                        Appointment.patient_id == context.patient.id,
                        Appointment.status == "scheduled",
                        Appointment.starts_at >= datetime.now(UTC),
                    )
                    .order_by(Appointment.starts_at.asc(), Appointment.id.asc())
                    .limit(MAX_PORTAL_APPOINTMENTS)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)
