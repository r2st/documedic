"""Appointment and ProviderAvailability — the clinic diary.

Why a diary belongs in a record system that is otherwise about what already happened: every
other artefact here is retrospective. Encounters record visits that occurred, labs record
samples that were drawn, handovers record patients already handed over. The two clinical
workflows this round adds — an order set's follow-up interval and a patient's own view of
their care — both need to say *when next*, and until now there was nowhere to put that. A
follow-up written into a consultation note as "review in 6 weeks" is a follow-up nobody is
listed for.

**Conflict detection lives in the service and is only half-enforceable in the schema.** Two
appointments overlap when their intervals intersect, and no unique index can express that:
PostgreSQL can, with an ``EXCLUDE ... USING gist (tstzrange(...) WITH &&)`` constraint, but
that needs the ``btree_gist`` extension created by a superuser, which this deployment's
migration user is not. What the schema *can* hold is the exact-collision case —
``uq_appointments_provider_start`` refuses a second live booking of the same provider at the
same instant — and that is the shape a double-submitted booking form takes, which is the race
the service's read-then-insert check cannot close on its own. Partial overlap remains a
service-level guarantee, and :func:`app.core.scheduling.find_conflicts` is where it lives.

**Cancelling frees the slot; nothing else does.** ``OCCUPYING_STATUSES`` is ``("scheduled",)``
— see the constant for why ``completed`` and ``no_show`` are excluded rather than relied on to
be in the past.

**The provider is a name, not an account id**, for the reason the handover's two clinicians
are: this product's deployment model is one practice login held signed in across a shift and
several machines, so ``account_id`` identifies the practice and not the person whose diary
this is. ``created_by_account_id`` records which login made the booking, which is provenance
rather than identity. The same limitation the step-up gate documents.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.scheduling import APPOINTMENT_MODALITIES, APPOINTMENT_STATUSES
from app.db.types import GUID
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

_STATUS_VALUES = ", ".join(repr(s) for s in APPOINTMENT_STATUSES)
_MODALITY_VALUES = ", ".join(repr(m) for m in APPOINTMENT_MODALITIES)

# An interval, or it is not an appointment. Stated as a constraint rather than left to the
# service because a zero-length or inverted slot overlaps *nothing* — it can be booked on top
# of every other appointment in the diary without limit, and it appears on no list that filters
# by "covers this hour". A validation bug that produced one would be invisible until somebody
# asked why the eleven o'clock was double-booked.
SLOT_IS_AN_INTERVAL = "ends_at > starts_at"

# A cancellation names a time and a reason, or the row is not cancelled. Half of the pair is a
# cancellation nobody can date or a reason attached to a live booking.
CANCELLATION_COMPLETE = "(status = 'cancelled') = (cancelled_at IS NOT NULL)"


class Appointment(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """One booked slot for one patient with one named provider."""

    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="ck_appointments_status"),
        CheckConstraint(
            f"modality IN ({_MODALITY_VALUES})",
            name="ck_appointments_modality",
        ),
        CheckConstraint(SLOT_IS_AN_INTERVAL, name="ck_appointments_slot_interval"),
        CheckConstraint(CANCELLATION_COMPLETE, name="ck_appointments_cancellation_complete"),
        # The exact-collision half of conflict detection — see the module docstring for what
        # this can and cannot hold. Two live bookings of one provider starting at the same
        # instant is what a double-submitted form produces, and it is the case the service's
        # read-then-insert check is least able to catch on its own.
        #
        # Scoped by account as well as provider: two practices may each employ a "Dr Sharma",
        # and this product's tenancy means neither can see the other's diary, so a cross-account
        # collision would be a refusal with nothing behind it.
        #
        # The status list is spelled out literally rather than interpolated into ``text()``:
        # tests/test_sql_injection_surface.py pins that every ``text()`` in this codebase takes
        # a literal, and ``test_the_occupancy_predicate_matches_the_occupying_statuses`` holds
        # the two spellings together.
        Index(
            "uq_appointments_provider_start",
            "account_id",
            "provider_name",
            "starts_at",
            unique=True,
            sqlite_where=text("status IN ('scheduled') AND is_deleted = 0"),
            postgresql_where=text("status IN ('scheduled') AND is_deleted = false"),
        ),
        # The conflict read: WHERE account_id = ? AND provider_name = ? AND status = 'scheduled'
        # AND ends_at > ? AND starts_at < ?. Leads with the two equality columns and carries the
        # range column, which is the most an ordinary B-tree can do for an overlap query.
        Index(
            "ix_appointments_provider_window",
            "account_id",
            "provider_name",
            "starts_at",
            postgresql_where=text("is_deleted = false"),
        ),
        # The chart's own list: WHERE patient_id = ? ORDER BY starts_at DESC.
        Index(
            "ix_appointments_patient_start",
            "patient_id",
            text("starts_at DESC"),
            postgresql_where=text("is_deleted = false"),
        ),
        # The diary and the reminder derivation, both of which sweep the account's own
        # forthcoming bookings: WHERE account_id = ? AND starts_at BETWEEN ? AND ?.
        Index(
            "ix_appointments_account_start",
            "account_id",
            "starts_at",
            postgresql_where=text("is_deleted = false"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("accounts.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False, index=True
    )
    # Whose diary. A name as typed — see the module docstring for why this is not an account id.
    provider_name: Mapped[str] = mapped_column(String(200), nullable=False)

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduled", server_default="scheduled"
    )
    # How the visit will be conducted, decided at booking. Carries clinical weight rather than
    # administrative: an appointment booked as ``audio`` says in advance that the prescriber
    # will not have seen the patient, which is what ``app.core.telehealth`` turns into
    # prescribing restrictions once the encounter is written up.
    modality: Mapped[str] = mapped_column(
        String(20), nullable=False, default="in_person", server_default="in_person"
    )
    appointment_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Why the patient is coming, in the booker's words. Free text and deliberately optional: a
    # required reason on a booking screen is a reason typed as "review".
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set when this booking was created by applying a protocol template's follow-up interval,
    # so a later reader can tell a clinician's own booking from one an order set proposed.
    # Nullable and unconstrained in the other direction: most appointments have no template
    # behind them.
    source_protocol_key: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # The visit this booking was made at, when it was made during one. Not the visit it
    # *becomes*: an appointment is a plan and an encounter is a record of what happened, and
    # conflating them would let a booking be frozen by a signature it is not part of.
    booked_at_encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("encounters.id"), nullable=True
    )

    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Free text, and required of every cancellation by the service rather than by the schema —
    # ``ck_appointments_cancellation_complete`` pairs the status with the timestamp, because a
    # blank reason is a data-quality problem and a missing timestamp makes the row unreadable.
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Which login booked it. Provenance, not identity — see the module docstring.
    created_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=True
    )


class ProviderAvailability(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """One recurring block of a named provider's working week, in clinic-local time.

    Local rather than UTC because that is how a clinic states its hours — "Tuesdays, nine to
    one" does not move when the offset does — and because storing 03:30 UTC would make the row
    unreadable to the person maintaining it. The conversion happens once, in
    :func:`app.core.scheduling.availability_verdict`, against an offset the service passes in.

    **A provider with no rows here has unknown hours, not no hours.** That distinction is the
    reason this table exists at all rather than the hours being a free-text note: the scheduler
    has to be able to answer "outside working hours", "inside them", and "nobody has said", and
    only the third of those is safe to be silent about. See ``availability_verdict``.
    """

    __tablename__ = "provider_availability"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_provider_availability_weekday"),
        CheckConstraint(
            "start_minute >= 0 AND end_minute <= 1440 AND start_minute < end_minute",
            name="ck_provider_availability_window",
        ),
        # One row per provider per weekday per start time; a repeated window is a duplicate,
        # not a second clinic.
        Index(
            "uq_provider_availability_slot",
            "account_id",
            "provider_name",
            "weekday",
            "start_minute",
            unique=True,
            sqlite_where=text("is_deleted = 0"),
            postgresql_where=text("is_deleted = false"),
        ),
        Index(
            "ix_provider_availability_account_provider",
            "account_id",
            "provider_name",
            postgresql_where=text("is_deleted = false"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("accounts.id"), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Monday = 0 through Sunday = 6, matching ``datetime.weekday()`` so no translation table
    # exists to be got backwards.
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    # Minutes from local midnight. See ``AvailabilityWindow`` for why not ``time``.
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)
