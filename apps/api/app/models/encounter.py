"""Encounter model — clinical visits, source-linked to documents.

The lifecycle
-------------
``draft`` -> ``in_progress`` -> ``signed``, and a signed encounter may later be superseded by an
amendment, at which point it becomes ``amended``. The transitions and who may perform them live
in :class:`~app.services.encounter_service.EncounterService`; what is here is the part that has
to hold whatever calls it.

**Signing freezes the content.** After a clinician signs, the visit's clinical columns — the
date, the type, the presenting complaint, the notes, the chart it belongs to and the document it
came from — plus the signature itself can never change again. Enforced on PostgreSQL by
``trg_encounters_signed_frozen`` (migration 0031), which refuses the UPDATE at the database
rather than trusting every future writer to check first. This is the same judgement
``clinical_suggestions`` and ``drug_safety_overrides`` are held to by their own triggers
(Critical Safety Rule #7): a clinical statement a clinician attested to is not editable
afterwards, because a record that can be rewritten silently is not a record of what was decided.

**A correction is a new row.** ``amends_encounter_id`` points at the encounter this one
supersedes and ``amendment_reason`` says why, exactly as ``ClinicalSuggestion.supersedes_id``
does. The chain is linear and the schema keeps it that way: ``uq_encounters_one_signed_amendment``
admits only one *signed* amendment per original, so two clinicians amending the same visit
concurrently cannot both land and leave the chart with two competing "current" versions of one
consultation.

Encounters merged from an approved extraction start at ``draft``, not ``signed`` — see
``GraphService._merge_encounter``. Approving an extraction confirms a transcription; it is not a
clinician attesting to a visit note, and the two must not be recorded as the same act.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

# The lifecycle vocabulary, in one place, so the check constraint, the service's transition
# table and the schema enum cannot drift apart. Mirrored by ``EncounterStatus`` in
# app/schemas/common.py and by ``EncounterStatus`` in packages/shared-types/src/enums.ts —
# tests/test_shared_enums.py holds all three together.
ENCOUNTER_STATUSES: tuple[str, ...] = ("draft", "in_progress", "signed", "amended")

# The two statuses that mean "a clinician has attested to this". Content is frozen from the
# moment a row reaches either of them; ``amended`` is still signed, it merely has a successor.
SIGNED_STATUSES: tuple[str, ...] = ("signed", "amended")

# Columns a signature freezes. Everything a clinician attested to, plus the attestation itself
# and the amendment link. Deliberately NOT ``status`` (signed -> amended is bookkeeping the
# amendment performs), ``updated_at`` (moves with any of those), or the soft-delete pair (chart
# withdrawal has to keep working on a chart holding signed visits).
FROZEN_ON_SIGN: tuple[str, ...] = (
    "patient_id",
    "source_document_id",
    "encounter_date",
    "encounter_type",
    "presenting_complaint",
    "clinician_notes",
    "signed_at",
    "signed_by_account_id",
    "amends_encounter_id",
    "amendment_reason",
)

_STATUS_VALUES = ", ".join(repr(s) for s in ENCOUNTER_STATUSES)
_SIGNED_VALUES = ", ".join(repr(s) for s in SIGNED_STATUSES)

# A signature is a person and a time or it is not a signature. Stated as a constraint because
# the three columns are written by three different paths (sign, amend, and the extraction merge
# that creates drafts) and a signed row missing its signer is indistinguishable, on read, from
# one nobody has attested to.
SIGNATURE_COMPLETE = (
    f"(status IN ({_SIGNED_VALUES})) = (signed_at IS NOT NULL AND signed_by_account_id IS NOT NULL)"
)

# An amendment names what it supersedes and why, or it is an ordinary encounter. Half of either
# pair is a correction whose target or whose reason nobody can read back.
AMENDMENT_COMPLETE = "(amends_encounter_id IS NULL) = (amendment_reason IS NULL)"

# ``amended`` is the one status that records something happening *to* the row after it was
# signed, so it carries its own timestamp; the other three must not.
AMENDED_AT_SET = "(status = 'amended') = (amended_at IS NOT NULL)"


class Encounter(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Clinical encounter/visit."""

    __tablename__ = "encounters"
    __table_args__ = (
        CheckConstraint(
            "encounter_type IS NULL OR encounter_type IN "
            "('outpatient', 'inpatient', 'emergency', 'teleconsultation', 'follow_up', 'other')",
            name="ck_encounters_type",
        ),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="ck_encounters_status"),
        CheckConstraint(SIGNATURE_COMPLETE, name="ck_encounters_signature_complete"),
        CheckConstraint(AMENDMENT_COMPLETE, name="ck_encounters_amendment_complete"),
        CheckConstraint(AMENDED_AT_SET, name="ck_encounters_amended_at"),
        # An encounter cannot amend itself. Trivially true of the service, which builds the
        # amendment as a new row, and cheap to state — a self-referencing chain is a walk that
        # never terminates for every reader that follows the link.
        CheckConstraint(
            "amends_encounter_id IS NULL OR amends_encounter_id <> id",
            name="ck_encounters_amendment_not_self",
        ),
        # The durable half of "one amendment wins". ``EncounterService.sign`` locks the target
        # row before flipping it to ``amended``, which serialises two concurrent signings on
        # PostgreSQL — but SQLAlchemy's SQLite dialect drops FOR UPDATE silently, and a lock is
        # in any case a claim about one process. This index holds on every backend: the second
        # signed amendment of one visit fails its INSERT/UPDATE rather than leaving the chart
        # with two successors to the same consultation and no rule for which one a clinician is
        # reading. Drafts are unconstrained on purpose — two clinicians may both *start* an
        # amendment, and only one can finish it.
        # The status list is spelled out literally rather than interpolated from
        # ``SIGNED_STATUSES``: ``text()`` must take a literal string everywhere in this codebase
        # (tests/test_sql_injection_surface.py pins that, and an f-string inside ``text()`` is
        # the shape every SQL injection has ever had, whether or not this particular one could
        # be reached). The two are held together by
        # ``test_the_amendment_index_predicate_matches_the_signed_statuses``.
        Index(
            "uq_encounters_one_signed_amendment",
            "amends_encounter_id",
            unique=True,
            sqlite_where=text(
                "amends_encounter_id IS NOT NULL AND status IN ('signed', 'amended') "
                "AND is_deleted = 0"
            ),
            postgresql_where=text(
                "amends_encounter_id IS NOT NULL AND status IN ('signed', 'amended') "
                "AND is_deleted = false"
            ),
        ),
        # The list read: WHERE patient_id = ? AND is_deleted = false ORDER BY encounter_date DESC.
        # Encounters accumulate one row per visit, so this is the smallest of the chart's
        # sections — the index is here because the read is paged and therefore has something to
        # stop early on, not because the table is large.
        Index(
            "ix_encounters_patient_date_live",
            "patient_id",
            text("encounter_date DESC"),
            postgresql_where=text("is_deleted = false"),
        ),
    )

    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False, index=True
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=True
    )
    encounter_date: Mapped[date] = mapped_column(Date, nullable=False)
    encounter_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    presenting_complaint: Mapped[str | None] = mapped_column(Text, nullable=True)
    clinician_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_region: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    extraction_confidence: Mapped[dict] = mapped_column(
        JSONBType, nullable=False, default=dict, server_default="{}"
    )
    # Not indexed on its own: every read that filters on status also filters on patient_id, and
    # ix_encounters_patient_date_live leads with that. Four values across a table of visits would
    # not be selective enough to be worth a scan of its own in any case.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft"
    )
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The account that attested, not the account that typed. Nullable because a draft has no
    # signer, and held to that by ``ck_encounters_signature_complete``.
    signed_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=True
    )
    # Set on the *original* when its amendment is signed — the moment the correction became the
    # current version of the visit, which is not the moment the amendment was drafted.
    amended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    amends_encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("encounters.id"), nullable=True
    )
    # Free text, and required of every amendment: "what changed and why" is the whole of what a
    # formal amendment adds over an edit. Same rule as ``DrugSafetyOverride.reasoning`` —
    # see EncounterService.amend for the blank-reason check.
    amendment_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
