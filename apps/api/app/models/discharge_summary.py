"""DischargeSummary — the take-home list, reconciled, attested to, and written to the chart.

The schema held nothing about a discharge. It could record an admission (``encounters``), what
was prescribed during it (``medication_events``) and a handover between clinicians
(``patient_handoffs``), but the transition where the patient leaves — which is the transition
that produces the discrepancies — left no artefact at all. What the patient actually went home
on was recoverable only by inferring it from whatever rows happened to be ``is_current``, which
is precisely the list that was wrong.

Three decisions are worth not re-deriving.

**Finalising writes to the chart.** This is the difference between a document and a record. A
discharge summary that lists five medicines while ``medication_events`` still carries the
admission's seven is not a discrepancy the next clinician can see — the chart looks complete,
and every safety check at the next visit runs against a list that has been wrong since the day
the patient left. ``medication_event_ids`` holds the rows finalising created, so the chart
change and the document that caused it point at each other in both directions.

**Every discontinuation is confirmed, never inferred.** ``app.core.med_reconciliation`` is
explicit that a ``stop`` disposition is a statement about two lists, not an instruction: it
means the discharge list does not carry a drug the chart calls current, which is an intended
discontinuation about half the time and a line somebody forgot to type the other half.
``confirmed_stops`` records the discontinuations the clinician named, the service recomputes the
set at finalisation and refuses on any difference — the same recompute-and-compare
``HandoffService.send`` performs, and for the same reason.

**Finalising freezes the content**, exactly as signing freezes an encounter and sending freezes
a handover. The narrative, the medication list, the reconciliation snapshot, the readiness
snapshot and the chart rows it wrote can never change afterwards. Enforced by trigger on
PostgreSQL (migration 0044) rather than by trusting every future writer, because a discharge
summary that a later code path can quietly rewrite is not evidence of what the patient was told.
A correction is a new summary naming the one it supersedes, the same shape ``ClinicalSuggestion``
and ``Encounter`` amendments already use (Critical Safety Rule #7).

**One finalised summary per encounter.** ``uq_discharge_summaries_one_final_per_encounter``
admits a single finalised row per admission, so two clinicians finalising the same discharge
concurrently cannot both land and leave the chart with two competing accounts of what the
patient went home on. Drafts are unconstrained: several people may start writing one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.discharge import DISCHARGE_STATUSES
from app.db.types import GUID, JSONBType
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

# The statuses that mean "a clinician has attested to this and the chart has been written".
# A tuple of one today; kept in the same shape as ``SENT_STATUSES`` and ``SIGNED_STATUSES`` so
# the freeze trigger and the service's transition table read identically across the three.
FINAL_STATUSES: tuple[str, ...] = ("finalized",)

# Columns finalising freezes: everything the clinician attested to, the reconciliation and
# readiness snapshots that justified it, the chart rows it wrote, and the attestation itself.
#
# Deliberately NOT ``status`` (there is nowhere further for it to go today, and the trigger
# refuses a move backwards separately), ``updated_at``, or the soft-delete pair — withdrawing a
# chart has to keep working on one holding finalised discharges.
FROZEN_ON_FINALIZE: tuple[str, ...] = (
    "patient_id",
    "encounter_id",
    "admission_reason",
    "hospital_course",
    "discharge_diagnosis",
    "follow_up_instructions",
    "patient_instructions",
    "discharge_medications",
    "reconciliation",
    "readiness",
    "confirmed_stops",
    "medication_event_ids",
    "supersedes_id",
    "correction_reason",
    "finalized_at",
    "finalized_by",
    "finalized_by_account_id",
)

_STATUS_VALUES = ", ".join(repr(s) for s in DISCHARGE_STATUSES)
_FINAL_VALUES = ", ".join(repr(s) for s in FINAL_STATUSES)

# A finalised summary names a clinician and carries a time, or it is still a draft. Stated as a
# constraint for the same reason the encounter signature is: a finalised row missing its
# finaliser is indistinguishable, on read, from one nobody has attested to, and who sent this
# patient home on this list is the entire clinical meaning of the row.
FINALIZATION_COMPLETE = (
    f"(status IN ({_FINAL_VALUES})) = "
    "(finalized_at IS NOT NULL AND finalized_by IS NOT NULL "
    "AND finalized_by_account_id IS NOT NULL)"
)

# A correction says what it corrects and why, or it is not a correction. Both columns or
# neither: a ``supersedes_id`` with no reason is a replacement nobody has to justify, and a
# reason pointing at nothing is a note.
CORRECTION_COMPLETE = "(supersedes_id IS NULL) = (correction_reason IS NULL)"


class DischargeSummary(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """One discharge, from draft through the reconciliation that justified finalising it."""

    __tablename__ = "discharge_summaries"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="ck_discharge_status"),
        CheckConstraint(FINALIZATION_COMPLETE, name="ck_discharge_finalization_complete"),
        CheckConstraint(CORRECTION_COMPLETE, name="ck_discharge_correction_complete"),
        # One finalised account of one admission, so two clinicians finalising the same
        # discharge concurrently cannot both land and leave the chart with two competing
        # versions of what the patient went home on. Drafts are unconstrained: several people
        # may start writing one.
        #
        # Spelled for both dialects, as ``uq_encounters_one_signed_amendment`` is. Without the
        # SQLite predicate the index is built *unpartitioned* on SQLite — the ``postgresql_``
        # prefix means the clause is simply dropped — and the constraint would then refuse a
        # second draft against one admission, and refuse the second *outpatient* summary on any
        # chart, since every one of those carries a NULL encounter_id. The predicates say the
        # same thing in the two dialects' spellings of false; they are held together by
        # ``test_the_uniqueness_predicate_reads_the_same_in_both_dialects``.
        Index(
            "uq_discharge_summaries_one_final_per_encounter",
            "encounter_id",
            unique=True,
            sqlite_where=text(
                "encounter_id IS NOT NULL AND status = 'finalized' AND is_deleted = 0"
            ),
            postgresql_where=text(
                "encounter_id IS NOT NULL AND status = 'finalized' AND is_deleted = false"
            ),
        ),
        # The list read: WHERE patient_id = ? AND is_deleted = false ORDER BY created_at DESC.
        Index(
            "ix_discharge_patient_created_live",
            "patient_id",
            text("created_at DESC"),
            postgresql_where=text("is_deleted = false"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("accounts.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False, index=True
    )
    # The admission this closes. Nullable because this product also serves outpatient practice,
    # where a course of care ends without an inpatient encounter to point at — and a required
    # column would be satisfied by pointing at the nearest visit, which is worse than a null.
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("encounters.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")

    # --- The narrative. Separate columns rather than one note for the same reason SBAR has
    # four: the sections that get dropped are the last two, and a schema that lets them be
    # empty lets them be forgotten. ``discharge_diagnosis`` and ``hospital_course`` are refused
    # empty at finalisation (``app.core.discharge.REQUIRED_SECTIONS``); the rest are advisory.
    admission_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    hospital_course: Mapped[str | None] = mapped_column(Text, nullable=True)
    discharge_diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)
    follow_up_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Written for the patient rather than for the next clinician. Held apart from the follow-up
    # instructions because the two have different readers and the patient portal shows only this
    # one — see ``app.core.portal_redaction``.
    patient_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- The medication list, as submitted: name, dose, unit, frequency per line. Stored on the
    # draft so that previewing and finalising read the same list, and so a discharge written
    # over two days does not lose it.
    discharge_medications: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)

    # The reconciliation at the moment of finalising: every line's disposition, the flags about
    # the list, and the counts. A snapshot rather than a live view, for the same reason the
    # handover checklist is one — the record's job is to say what was true when the patient went
    # home, and a table that re-derived itself on read would show today's chart.
    reconciliation: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    # The readiness items that stood at finalisation, blocking ones included. A finalised
    # summary never carries a blocking item — it could not have been finalised — so what this
    # holds in practice is the advisory list the clinician proceeded past, which is exactly the
    # thing a later review asks about.
    readiness: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    # The discontinuations the clinician named, folded, as compared against the recomputed set.
    confirmed_stops: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    # The ``medication_events`` rows finalising wrote. The chart change and the document that
    # caused it point at each other; without this the events are indistinguishable from
    # prescribing typed in one line at a time.
    medication_event_ids: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)

    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The clinician's own name, as typed. Not an account id, for the reason
    # ``app.models.handoff`` states at length: the deployment model is one practice login held
    # across a shift, so the account identifies the practice rather than the person.
    finalized_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Which login it was finalised through. Provenance, not identity.
    finalized_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=True
    )

    # A correction is a new row pointing at the one it replaces, never an edit (Rule #7).
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("discharge_summaries.id"), nullable=True
    )
    correction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
