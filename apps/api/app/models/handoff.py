"""PatientHandoff — a structured SBAR handover, with the chart's own risks on the checklist.

Handover is where information is lost. The SBAR structure (Situation, Background, Assessment,
Recommendation) exists because unstructured verbal handover reliably drops the Assessment and
the Recommendation, and what survives is a name and a diagnosis. Four named columns are the
cheap half of fixing that.

The half that is not cheap is the **checklist**, and the design decision worth not re-deriving
is that its items are *computed from the chart* rather than typed by the clinician. A
free-form checklist is a list of the things the outgoing clinician remembered, which is exactly
the faculty handover is failing. What is on this one is what this chart currently carries that
the incoming clinician would want to know and cannot see from the SBAR text: unacknowledged
critical lab values, hard blocks a clinician has overridden, documented allergies, how many
medications are live, documents still awaiting review. Each non-zero item has to be confirmed
before the handoff can be sent; a zero item is not on the list at all, because there is nothing
to hand over and a checkbox for it is a checkbox that trains people to tick boxes.

**The checklist is recomputed at send time and compared against what was confirmed.** A
handoff drafted at 6pm and sent at 8pm may be describing a chart that has since acquired a
panic potassium, and a confirmation of a state that no longer holds is worse than no
confirmation: it is a signed statement that somebody checked. A send whose recomputed checklist
differs is refused and the clinician re-confirms. Same judgement — and the same failure mode —
as ``reasoning_chart_changed_under_run``.

**Sending freezes the content**, exactly as signing freezes an encounter: the SBAR text, the
checklist snapshot, the two clinician names and the chart it belongs to cannot change
afterwards. A handover note that can be rewritten after the fact is not a record of what was
handed over. Enforced by trigger on PostgreSQL (migration 0037) rather than by trusting every
future writer, the same way ``encounters``, ``clinical_suggestions`` and
``drug_safety_overrides`` are held.

**Why the clinicians are names and not account ids.** This product's tenancy is account-scoped:
a patient belongs to one account, and ``PatientService.get`` is what stops one account reading
another's charts. A handoff addressed to a *different* account would therefore be addressed to
somebody who cannot open the chart it is about — the SBAR would arrive and the record behind it
would 404. And within an account the id says nothing useful either: the deployment model is one
practice login held signed in across a shift and several machines, so ``account_id`` identifies
the practice, not the person going off shift. Both clinicians are therefore recorded by name,
as typed, and ``acknowledged_by_account_id`` records which login the acknowledgement came
through — which is provenance, not identity. This is the same limitation the step-up
re-authentication gate documents, and it is stated here rather than papered over.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

# The lifecycle, in one place, so the check constraint, the service's transition table and the
# schema enum cannot drift apart. Mirrored by ``HandoffStatus`` in app/schemas/handoff.py.
HANDOFF_STATUSES: tuple[str, ...] = ("draft", "sent", "acknowledged")

# The statuses that mean "this has left the outgoing clinician's hands". Content is frozen from
# the moment a row reaches either of them; ``acknowledged`` is still sent, it merely has a
# receipt.
SENT_STATUSES: tuple[str, ...] = ("sent", "acknowledged")

# Columns sending freezes. Everything the outgoing clinician asserted, plus the assertion itself.
# Deliberately NOT ``status`` (sent -> acknowledged is the receipt doing its job),
# ``updated_at`` (moves with any of those), the acknowledgement columns (they are written
# *after* the freeze, by the receiving clinician), or the soft-delete pair — chart withdrawal
# has to keep working on a chart holding sent handoffs.
FROZEN_ON_SEND: tuple[str, ...] = (
    "patient_id",
    "situation",
    "background",
    "assessment",
    "recommendation",
    "from_clinician",
    "to_clinician",
    "checklist",
    "sent_at",
)

_STATUS_VALUES = ", ".join(repr(s) for s in HANDOFF_STATUSES)
_SENT_VALUES = ", ".join(repr(s) for s in SENT_STATUSES)

# A sent handoff names both clinicians and carries a time, or it is still a draft. Stated as a
# constraint because a sent row missing its sender is indistinguishable, on read, from one
# nobody has sent — and the whole clinical meaning of the row is who handed over to whom.
SEND_COMPLETE = (
    f"(status IN ({_SENT_VALUES})) = "
    "(sent_at IS NOT NULL AND from_clinician IS NOT NULL AND to_clinician IS NOT NULL)"
)

# An acknowledgement is a person and a time, or it is not an acknowledgement. Same shape and
# same reasoning as ``encounters``' signature constraint.
ACKNOWLEDGEMENT_COMPLETE = (
    "(status = 'acknowledged') = (acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL)"
)


class PatientHandoff(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """One SBAR handover of one patient, from one named clinician to another."""

    __tablename__ = "patient_handoffs"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="ck_handoffs_status"),
        CheckConstraint(SEND_COMPLETE, name="ck_handoffs_send_complete"),
        CheckConstraint(ACKNOWLEDGEMENT_COMPLETE, name="ck_handoffs_ack_complete"),
        # The list read: WHERE patient_id = ? AND is_deleted = false ORDER BY created_at DESC.
        Index(
            "ix_handoffs_patient_created_live",
            "patient_id",
            text("created_at DESC"),
            postgresql_where=text("is_deleted = false"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("accounts.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")

    # --- SBAR. Four columns rather than one note, because the two that get dropped in verbal
    # handover are the last two, and a schema that lets them be empty lets them be forgotten.
    situation: Mapped[str] = mapped_column(Text, nullable=False)
    background: Mapped[str] = mapped_column(Text, nullable=False)
    assessment: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)

    # Names, as typed. See the module docstring for why these are not account ids.
    from_clinician: Mapped[str | None] = mapped_column(String(200), nullable=True)
    to_clinician: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # The chart's outstanding risks at the moment of sending, and the clinician's confirmation
    # of each. A snapshot rather than a live view on purpose: the point of the record is what
    # was true when it was handed over, and a checklist that re-derives itself on read would
    # show the incoming clinician's chart rather than the outgoing one's.
    checklist: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The receiving clinician's own name, typed by them. The row below it says which login it
    # arrived through, which is provenance rather than identity.
    acknowledged_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    acknowledged_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=True
    )
    # What the receiving clinician said back, if anything — a query, a correction, "seen and
    # accepted". Optional for the same reason the critical-lab action note is: the required part
    # is that a named person received it.
    acknowledgement_note: Mapped[str | None] = mapped_column(Text, nullable=True)
