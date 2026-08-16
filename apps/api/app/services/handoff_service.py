"""SBAR handover: drafting, the chart-derived checklist, sending, and acknowledgement.

The checklist is the part with the design in it. See :func:`build_checklist`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.exceptions import (
    HandoffChecklistStaleError,
    HandoffNotSentError,
    HandoffSentError,
    NotFoundError,
)
from app.models.allergy import Allergy
from app.models.document import Document
from app.models.handoff import SENT_STATUSES, PatientHandoff
from app.models.medication_event import MedicationEvent
from app.services.audit_service import AuditService
from app.services.lab_safety_service import LabSafetyService
from app.services.patient_service import PatientService
from app.services.safety_service import SafetyService

# The checklist vocabulary. Each key is a thing this chart can be carrying that the incoming
# clinician needs told and cannot read off the SBAR prose, paired with how it is phrased to
# them. Closed rather than free-form on purpose — see ``build_checklist``.
#
# Phrased as statements of what is on the chart, never as instructions: "there are 2
# unacknowledged critical lab values" and not "review the critical lab values". A checklist
# that issues orders is a checklist a clinician disagrees with and then ignores wholesale.
CHECKLIST_ITEMS: dict[str, str] = {
    "unacknowledged_critical_labs": (
        "critical or panic lab value(s) on this chart that nobody has acknowledged"
    ),
    "active_hard_blocks": (
        "medication(s) currently charted that raise a safety hard block "
        "(a documented allergy or an absolute contraindication)"
    ),
    "documented_allergies": "documented allerg(ies) bearing on prescribing",
    "current_medications": "medication(s) the chart lists as current",
    "documents_needing_confirmation": (
        "uploaded document(s) that need a clinician to check the extraction against the original"
    ),
}


@dataclass(frozen=True)
class ChecklistItem:
    key: str
    count: int
    description: str

    def as_dict(self) -> dict:
        return {"key": self.key, "count": self.count, "description": self.description}


class HandoffService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    # --- The checklist -------------------------------------------------------------------

    async def build_checklist(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID
    ) -> list[ChecklistItem]:
        """What this chart is currently carrying that the incoming clinician must be told.

        **Computed, not typed.** A free-form handover checklist is a list of the things the
        outgoing clinician remembered — which is precisely the faculty that handover is
        failing. What is on this one is derived from the record: unacknowledged panic values,
        hard blocks standing against currently charted drugs, documented allergies, live
        medications, documents nobody has reviewed.

        **Zero-count items are omitted entirely** rather than listed as satisfied. A checklist
        that always shows five rows, of which three are permanently "0 — nothing to do", is a
        checklist people learn to tick without reading, and then the two that mattered are
        ticked the same way. What appears here is only ever something that is actually there.

        Deterministic and offline-capable throughout (Critical Safety Rule #8): five counts and
        the deterministic safety engine, no LLM. Handover happens at shift change, which is
        exactly when a degraded provider must not take a clinical workflow with it.
        """
        items: list[ChecklistItem] = []

        queue = await LabSafetyService(self.db).outstanding_critical_values(account_id=account_id)
        outstanding_here = sum(1 for lab, _flag in queue.entries if lab.patient_id == patient_id)
        if outstanding_here:
            items.append(_item("unacknowledged_critical_labs", outstanding_here))

        # The standing safety board for this chart — every current medication re-evaluated
        # against every other and against the patient. Only the hard blocks are counted: a
        # warning is context the incoming clinician will meet on the chart, whereas a hard block
        # is a drug that is charted *and* refused, which is a state somebody has to explain.
        flags = await SafetyService(self.db).active_flags(
            account_id=account_id, patient_id=patient_id
        )
        hard_blocks = sum(1 for _vocab, group in flags for f in group if f.is_hard_block)
        if hard_blocks:
            items.append(_item("active_hard_blocks", hard_blocks))

        allergies = await self._count(
            Allergy,
            Allergy.patient_id == patient_id,
            Allergy.is_deleted.is_(False),
            # "unknown" alongside "active", matching the safety engine's own predicate: an
            # allergy nobody has been able to confirm is the one a hard block exists for.
            Allergy.status.in_(("active", "unknown")),
        )
        if allergies:
            items.append(_item("documented_allergies", allergies))

        medications = await self._count(
            MedicationEvent,
            MedicationEvent.patient_id == patient_id,
            MedicationEvent.is_deleted.is_(False),
            MedicationEvent.is_current.is_(True),
        )
        if medications:
            items.append(_item("current_medications", medications))

        # ``needs_confirmation`` specifically, not "everything unapproved". That status is the
        # one the extraction path writes when a human has to look at the original before
        # anything from the page can be trusted — an unreadable scan, a dropped line, a
        # low-confidence field. A clean extraction nobody has got round to approving is also
        # unapproved, and putting it on a handover checklist beside a mangled warfarin line
        # would flatten the two into one number.
        pending = await self._count(
            Document,
            Document.patient_id == patient_id,
            Document.is_deleted.is_(False),
            Document.extraction_status == "needs_confirmation",
        )
        if pending:
            items.append(_item("documents_needing_confirmation", pending))

        return items

    async def _count(self, model: type, *predicates: ColumnElement[bool]) -> int:
        result = await self.db.execute(select(func.count()).select_from(model).where(*predicates))
        return int(result.scalar_one())

    # --- Lifecycle -----------------------------------------------------------------------

    async def create(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        situation: str,
        background: str,
        assessment: str,
        recommendation: str,
    ) -> PatientHandoff:
        """Draft a handover. Nothing is frozen and nothing has been handed to anyone yet."""
        await PatientService(self.db).get(account_id, patient_id)
        handoff = PatientHandoff(
            account_id=account_id,
            patient_id=patient_id,
            status="draft",
            situation=situation,
            background=background,
            assessment=assessment,
            recommendation=recommendation,
            checklist=[],
        )
        self.db.add(handoff)
        await self.db.flush()
        await self.audit.record(
            action="handoff_created",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="patient_handoff",
            entity_id=handoff.id,
            # Lengths, not text. The SBAR is a clinician's prose about a patient and the trail
            # is unencrypted, append-only and never pruned; ``entity_id`` leads to the row that
            # holds it. See tests/test_audit_payload_free_text.py.
            payload={"status": "draft", "sbar_chars": _sbar_chars(handoff)},
        )
        return handoff

    async def get(self, patient_id: uuid.UUID, handoff_id: uuid.UUID) -> PatientHandoff:
        handoff = await self.db.get(PatientHandoff, handoff_id)
        if handoff is None or handoff.is_deleted or handoff.patient_id != patient_id:
            raise NotFoundError(f"Handoff {handoff_id} not found on this patient")
        return handoff

    async def update(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        handoff_id: uuid.UUID,
        **fields: str | None,
    ) -> PatientHandoff:
        """Edit a draft. Refused once sent, like editing a signed encounter."""
        await PatientService(self.db).get(account_id, patient_id)
        handoff = await self.get(patient_id, handoff_id)
        if handoff.status in SENT_STATUSES:
            raise HandoffSentError(detail=f"edit refused: handoff {handoff_id} is {handoff.status}")
        for name, value in fields.items():
            if value is not None:
                setattr(handoff, name, value)
        await self.db.flush()
        await self.audit.record(
            action="handoff_updated",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="patient_handoff",
            entity_id=handoff.id,
            payload={"status": handoff.status, "sbar_chars": _sbar_chars(handoff)},
        )
        return handoff

    async def send(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        handoff_id: uuid.UUID,
        from_clinician: str,
        to_clinician: str,
        confirmed_checklist_keys: list[str],
    ) -> PatientHandoff:
        """Hand over. Freezes the content and snapshots the checklist.

        The checklist is **recomputed here** and compared against what the clinician confirmed.
        A handoff drafted at 6pm and sent at 8pm may be describing a chart that has since
        acquired a panic potassium; a confirmation of a state that no longer holds is worse than
        no confirmation, because it is a signed statement that somebody checked. A mismatch in
        either direction raises ``HandoffChecklistStaleError`` and the clinician re-reads.

        Both directions, not just "something new appeared": an item that has *gone away* — the
        critical lab acknowledged by someone else in the meantime — also means the clinician
        confirmed a picture that is not the current one, and the cheap answer of accepting the
        superset would let a stale confirmation through whenever the chart improved.
        """
        await PatientService(self.db).get(account_id, patient_id)
        handoff = await self.get(patient_id, handoff_id)
        if handoff.status in SENT_STATUSES:
            raise HandoffSentError(
                detail=f"send refused: handoff {handoff_id} is already {handoff.status}"
            )

        current = await self.build_checklist(account_id=account_id, patient_id=patient_id)
        required = {item.key for item in current}
        confirmed = set(confirmed_checklist_keys)
        if confirmed != required:
            raise HandoffChecklistStaleError(
                detail=(
                    f"checklist mismatch: confirmed={sorted(confirmed)} required={sorted(required)}"
                )
            )

        handoff.status = "sent"
        handoff.from_clinician = from_clinician
        handoff.to_clinician = to_clinician
        handoff.checklist = [item.as_dict() for item in current]
        handoff.sent_at = datetime.now(UTC)
        await self.db.flush()

        await self.audit.record(
            action="handoff_sent",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="patient_handoff",
            entity_id=handoff.id,
            payload={
                # The two clinician names are the *clinicians'* names, not the patient's, and
                # they are the entire clinical meaning of the entry: who handed this patient
                # over to whom, and when. The checklist goes in as its keys and counts —
                # structured, closed-vocabulary, and carrying nothing a clinician typed.
                "from_clinician": from_clinician,
                "to_clinician": to_clinician,
                "checklist": {item.key: item.count for item in current},
                "sbar_chars": _sbar_chars(handoff),
            },
        )
        return handoff

    async def acknowledge(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        handoff_id: uuid.UUID,
        acknowledged_by: str,
        note: str | None,
    ) -> PatientHandoff:
        """The receiving clinician's receipt. Closes the loop that verbal handover leaves open.

        Only a *sent* handoff can be acknowledged: a receipt for a draft records that somebody
        received something the outgoing clinician is still writing, and would then freeze it
        mid-sentence.

        Acknowledging twice is refused rather than appended, unlike a critical-lab
        acknowledgement. The difference is what the record means: a lab value can legitimately
        be seen by several clinicians and each attestation is worth keeping, whereas a handover
        is from one named person to one named person and a second receipt would leave the chart
        unable to say who took the patient on.
        """
        await PatientService(self.db).get(account_id, patient_id)
        handoff = await self.get(patient_id, handoff_id)
        if handoff.status == "acknowledged":
            raise HandoffSentError(
                detail=f"acknowledge refused: handoff {handoff_id} is already acknowledged"
            )
        if handoff.status != "sent":
            raise HandoffNotSentError(
                detail=f"acknowledge refused: handoff {handoff_id} is {handoff.status}"
            )

        handoff.status = "acknowledged"
        handoff.acknowledged_at = datetime.now(UTC)
        handoff.acknowledged_by = acknowledged_by
        # Which login the receipt arrived through. Provenance rather than identity — the
        # deployment model is one practice account, so this cannot be the answer to "who", which
        # is why ``acknowledged_by`` above is a typed name. See the model docstring.
        handoff.acknowledged_by_account_id = account_id
        handoff.acknowledgement_note = note
        await self.db.flush()

        await self.audit.record(
            action="handoff_acknowledged",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="patient_handoff",
            entity_id=handoff.id,
            payload={
                "acknowledged_by": acknowledged_by,
                "note_chars": len(note or ""),
                # How long the patient spent handed-over-but-unreceived. The interval is the
                # number a later review asks for, and it is not recoverable from two timestamps
                # in two different audit entries without knowing to look for the pair.
                "seconds_since_sent": _seconds_since(handoff.sent_at, handoff.acknowledged_at),
            },
        )
        return handoff

    async def list_for_patient(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID
    ) -> list[PatientHandoff]:
        await PatientService(self.db).get(account_id, patient_id)
        rows = await self.db.execute(
            select(PatientHandoff)
            .where(
                PatientHandoff.patient_id == patient_id,
                PatientHandoff.is_deleted.is_(False),
            )
            .order_by(PatientHandoff.created_at.desc(), PatientHandoff.id)
        )
        return list(rows.scalars().all())


def _item(key: str, count: int) -> ChecklistItem:
    return ChecklistItem(key=key, count=count, description=CHECKLIST_ITEMS[key])


def _sbar_chars(handoff: PatientHandoff) -> int:
    """The size of what was written, which is a fact about the note rather than its content."""
    return sum(
        len(getattr(handoff, name) or "")
        for name in ("situation", "background", "assessment", "recommendation")
    )


def _seconds_since(sent: datetime | None, acknowledged: datetime | None) -> int | None:
    """The interval between sending and acknowledgement, in whole seconds.

    Both operands are normalised to aware first. ``sent_at`` may come back from the database
    naive — SQLite drops tzinfo on round-trip, where PostgreSQL keeps it — while
    ``acknowledged_at`` was stamped in this process and is always aware. Subtracting the two
    raises, so a portable read has to do this; the same ``_aware`` shape ``AuthService`` uses.

    Clamped at zero, not because clock skew is expected within one process but because a
    negative interval in an append-only trail is a number a later reader cannot interpret.
    """
    if sent is None or acknowledged is None:
        return None
    return max(int((_aware(acknowledged) - _aware(sent)).total_seconds()), 0)


def _aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; treat naive timestamps as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
