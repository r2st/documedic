"""Patient CRUD, search, and consent-gated creation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ConsentRequiredError, ConsentWithdrawnError, PatientNotFoundError
from app.models.patient import Patient
from app.schemas.patient import PatientCreate, PatientUpdate
from app.services.audit_service import AuditService


class PatientService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def create(self, account_id: uuid.UUID, data: PatientCreate) -> Patient:
        if not data.consent_given:
            raise ConsentRequiredError()
        patient = Patient(
            account_id=account_id,
            full_name=data.full_name,
            date_of_birth=data.date_of_birth,
            sex=data.sex,
            phone=data.phone,
            address_text=data.address_text,
            notes=data.notes,
            consent_given=True,
            consent_given_at=datetime.now(UTC),
        )
        self.db.add(patient)
        await self.db.flush()
        await self.audit.record(
            action="patient_created",
            account_id=account_id,
            patient_id=patient.id,
            entity_type="patient",
            # AuditLog.payload is not itself encrypted, so PII (full_name etc.) must never
            # land here — only non-identifying facts about the action.
            entity_id=patient.id,
            payload={"consent_given": True},
        )
        await self.db.commit()
        await self.db.refresh(patient)
        return patient

    async def get(
        self, account_id: uuid.UUID, patient_id: uuid.UUID, *, for_update: bool = False
    ) -> Patient:
        """The patient, if this account owns it and the chart is still in use.

        ``for_update`` takes a row lock on the chart, and it means "serialise everything that
        merges into this record". Only :meth:`DocumentService.approve` asks for it. The merge
        decides what to insert by reading what the chart already holds, and the lock on the
        *document* row that ``approve`` already takes only serialises two approvals of the **same**
        document. Two different documents merging into one chart at the same time do not contend
        at all, and they are not independent work: a prescription that starts a drug and a
        prescription that stops it are the ordinary pair, and ``GraphService._retire_current``
        can only retire rows that are already committed. So the stop read a chart without the
        start on it, retired nothing, and the drug stayed ``is_current`` on a patient who had been
        taken off it — with a ``stop`` event on the same chart saying otherwise, which is exactly
        the disagreement between the FHIR export and the safety engine that
        ``is_current = event_type != "stop"`` was written to end.

        Serialising two approvals on one chart costs nothing real: they are human-paced and
        seconds apart at worst, and both still land — the loser waits and then reads the winner's
        rows, which is the sequential path the deduplication is already written for. Ordinary
        reads are untouched; PostgreSQL does not block a plain ``SELECT`` behind ``FOR UPDATE``.

        PostgreSQL only, in the same sense as ``DocumentService.get`` and ``AuditService._lock``:
        SQLAlchemy's SQLite dialect drops ``FOR UPDATE`` silently, so on a SQLite deployment two
        genuinely simultaneous approvals can still interleave.
        """
        statement = select(Patient).where(
            Patient.id == patient_id,
            Patient.account_id == account_id,
            Patient.is_deleted.is_(False),
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.db.execute(statement)
        patient = result.scalar_one_or_none()
        if patient is None:
            raise PatientNotFoundError()
        return patient

    async def get_for_audit(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
        """Ownership check for reading the audit trail — **including withdrawn charts**.

        :meth:`get` without the ``is_deleted`` predicate, and the only caller is the audit
        router. Withdrawing a chart soft-deletes it precisely so the rows survive underneath:
        the trail is append-only and its hash chain references them, and ``soft_delete``
        appends a ``patient_deleted`` entry to that same trail. Gating the read on
        ``is_deleted is False`` meant the withdrawal made its own record unreachable, along
        with every access, suggestion and override recorded before it — and made
        ``GET ../audit/verify``, the tamper-evidence check, unrunnable for exactly the charts
        most likely to be disputed. A trail that disappears when the record is withdrawn is
        not an audit trail.

        Safe to widen *here* and nowhere else because of what the trail holds. Payloads are
        deliberately identifiers and counts rather than clinical text (see the comments on
        ``patient_created`` and ``reasoning_session_started``), so this discloses that a chart
        existed and what was done to it, not what it said. The clinical content — the record,
        the safety flags, the suggestions — stays behind :meth:`get` and stays 404 once the
        chart is withdrawn.

        Account scoping is unchanged: another clinician's withdrawn chart is still a 404.
        """
        result = await self.db.execute(
            select(Patient).where(
                Patient.id == patient_id,
                Patient.account_id == account_id,
            )
        )
        patient = result.scalar_one_or_none()
        if patient is None:
            raise PatientNotFoundError()
        return patient

    async def get_for_processing(
        self, account_id: uuid.UUID, patient_id: uuid.UUID, *, for_update: bool = False
    ) -> Patient:
        """Fetch a patient for an operation that *processes new personal data*.

        :meth:`get` plus the lawful-basis check. Consent was required to create the chart and
        then never read again, so setting ``consent_given`` back to false recorded a withdrawal
        that changed nothing: documents still ingested, the reasoning engine still ran. Under
        the DPDP Act 2023 a data principal may withdraw at any time and processing has to
        stop, and ``regulatory_service`` asserts exactly that control in the compliance summary
        it publishes.

        Used by the three entry points that add to the record or run the engine over it —
        document upload, extraction approval, and opening a reasoning session. Everything else
        keeps calling :meth:`get`: withdrawal is not erasure, and see
        :class:`~app.exceptions.ConsentWithdrawnError` for why the safety checks in particular
        must not be behind this gate.

        ``for_update`` passes straight through to :meth:`get`, which documents what the lock is
        for. Only extraction approval asks for it.
        """
        patient = await self.get(account_id, patient_id, for_update=for_update)
        if not patient.consent_given:
            raise ConsentWithdrawnError(detail=f"patient {patient_id} has consent_given=False")
        return patient

    async def get_for_display(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
        """Fetch a patient *and* record the PHI access in the audit trail.

        Separate from :meth:`get` on purpose. ``get`` is also the ownership check that nearly
        every other router calls before doing its own work, so auditing inside it would file a
        access entry for each of those and drown the real signal. This variant is for the
        endpoints that actually hand decrypted patient data to a caller.

        Writes were already audited; reads were not, which left the most common real-world
        breach -- a legitimate account browsing records it has no clinical reason to open --
        invisible. The DPDP Act's accountability duty needs read access to be attributable too.
        """
        patient = await self.get(account_id, patient_id)
        await self.audit.record(
            action="patient_viewed",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="patient",
            entity_id=patient_id,
        )
        await self.db.commit()
        return patient

    async def list(
        self,
        account_id: uuid.UUID,
        *,
        search: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[Patient], int]:
        """List/search an account's patients.

        full_name and phone are encrypted at rest (non-deterministic ciphertext), so they can't
        be filtered with SQL ILIKE/LIKE any more. When ``search`` is set this loads the
        account's patients (already scoped to one clinician's panel, not the whole system) and
        filters in Python after decryption, then paginates the filtered list. Without a search
        term, pagination stays a plain SQL LIMIT/OFFSET as before.

        **The ordering ends in the primary key**, for the reason
        :mod:`app.services.record_service` spells out and every other paged read in this codebase
        already observed: ``LIMIT``/``OFFSET`` over a sort with ties is not a partition of the
        set. ``updated_at`` alone is full of ties here — a seeded panel, a bulk import, or any
        two charts created inside the same clock tick share it — and two rows that compare equal
        may come back in either order between two requests, so paging the list could serve one
        patient twice and never show another at all. The tiebreaker is clinically meaningless
        and that is the point: it exists to make the comparison total, not to be read.

        ``ix_patients_account_updated_live`` does not carry ``id``, so on PostgreSQL the planner
        takes an incremental sort over the index prefix rather than a plain ordered scan. That is
        a sort of the rows sharing one ``updated_at`` value, not of the page, and it is the price
        of the page meaning anything.
        """
        base = select(Patient).where(
            Patient.account_id == account_id, Patient.is_deleted.is_(False)
        )

        if search:
            needle = search.strip().lower()
            result = await self.db.execute(base.order_by(Patient.updated_at.desc(), Patient.id))
            matched = [
                p
                for p in result.scalars().all()
                if needle in (p.full_name or "").lower() or needle in (p.phone or "")
            ]
            total = len(matched)
            return matched[offset : offset + limit], total

        total_count = await self.db.scalar(select(func.count()).select_from(base.subquery()))
        result = await self.db.execute(
            base.order_by(Patient.updated_at.desc(), Patient.id).limit(limit).offset(offset)
        )
        return list(result.scalars().all()), int(total_count or 0)

    async def update(
        self, account_id: uuid.UUID, patient_id: uuid.UUID, data: PatientUpdate
    ) -> Patient:
        """Amend the demographics a clinician sent, and only those.

        **What happens when two clinicians edit the same chart at once**, since the arrangement
        this product is built for is one practice login used from two rooms and nothing here
        takes a lock:

        * Only fields present in the request body are written (``exclude_unset``). Two clinicians
          amending *different* fields both keep their edit — this is a field-level merge, not a
          whole-record overwrite, and it is the case that actually occurs when one corrects a
          phone number while the other records consent.
        * Two clinicians amending the *same* field is last-write-wins. There is no version token
          to reject the second write with, and inventing one here would be the wrong trade: a
          409 on a demographic correction sends the clinician back to a form they have already
          filled in, to resolve a conflict that is nearly always "we both fixed the same typo".
        * Both writes are audited either way, each naming the fields it changed, so the losing
          edit is recoverable from the trail rather than lost without trace. That is what makes
          last-write-wins acceptable here and would not make it acceptable for clinical content.

        Clinical content — medications, labs, conditions, allergies — never arrives this way. It
        is merged from an approved document extraction, and *that* path does serialise: see
        ``DocumentService.approve``, which locks the chart because a merge decides what to write
        by reading what is already there, and two of them interleaving leaves a drug both stopped
        and current.

        Pinned by ``tests/test_concurrent_chart_edits.py``.
        """
        patient = await self.get(account_id, patient_id)
        changed: dict = {}
        for field_name, value in data.model_dump(exclude_unset=True).items():
            if field_name == "consent_given":
                if value and not patient.consent_given:
                    patient.consent_given_at = datetime.now(UTC)
                patient.consent_given = bool(value)
                changed["consent_given"] = bool(value)
                continue
            setattr(patient, field_name, value)
            changed[field_name] = value
        await self.db.flush()
        await self.audit.record(
            action="patient_updated",
            account_id=account_id,
            patient_id=patient.id,
            entity_type="patient",
            entity_id=patient.id,
            # The payload carries changed field *names*, never their values -- audit_logs is
            # unencrypted, so a new full_name must not land here. `consent_given` is the one
            # exception, and it is the opposite of PII: it is the lawful basis for holding
            # everything else. Without it the trail said only that consent had been "changed",
            # so a grant and a withdrawal were the same entry, and the one record that proves
            # when processing became unlawful could not be read back from it.
            payload={
                "changed_fields": sorted(changed.keys()),
                # The new consent state when this edit changed it, else None. Kept a dict
                # literal so the payload-contract sweep in test_audit_payload_free_text can
                # still read the keys statically -- this is the call site that writes whatever
                # fields a user edited, so it is the last one that should become opaque to it.
                "consent_given": changed.get("consent_given"),
            },
        )
        await self.db.commit()
        await self.db.refresh(patient)
        return patient

    async def soft_delete(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> None:
        patient = await self.get(account_id, patient_id)
        patient.is_deleted = True
        patient.deleted_at = datetime.now(UTC)
        await self.db.flush()
        await self.audit.record(
            action="patient_deleted",
            account_id=account_id,
            patient_id=patient.id,
            entity_type="patient",
            entity_id=patient.id,
            payload={"soft_delete": True},
        )
        await self.db.commit()
