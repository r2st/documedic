"""LabResult model — lab test results with reference ranges, source-linked."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


def _canonical_number(value: object) -> str:
    """A numeric column value rendered so that numerically equal values render identically.

    ``Decimal("3.0") == Decimal("3.000000")`` but ``str()`` of the two differs, and both spellings
    genuinely occur: the first is what the merge parses out of an extraction, the second is what
    ``Numeric(18, 6)`` hands back on the round trip. A key built on the raw text would call the
    same observation two different things. ``normalize()`` strips the trailing zeros, and the
    ``f`` format keeps the result out of exponent notation (``Decimal("1E+2")`` normalizes to
    ``1E+2``, which would then not match the ``100`` the same value arrives as elsewhere).

    Non-``Decimal`` input is accepted because this runs as a column default over whatever was
    handed to the INSERT, and a ``float`` or a numeric string reaches a ``Numeric`` column
    perfectly happily. Anything that is not a number at all falls back to its text, which keeps
    the key defined (and still distinguishing) rather than failing the insert.
    """
    if value is None:
        return ""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (ArithmeticError, ValueError):
        return str(value)


def _canonical_timestamp(value: object) -> str:
    """A sample timestamp rendered as UTC text, or "" when there is none.

    ``datetime`` is the column's type and the case that matters. A bare ``date`` is handled
    because it also reaches a ``DateTime`` column intact (SQLAlchemy passes it through and the
    driver widens it), and ``date`` has no ``tzinfo`` to normalise — reading one would raise
    inside a column default, which would fail an insert that has nothing to do with dedup.
    """
    if value is None:
        return ""
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


def lab_observation_key(
    source_document_id: object,
    marker_name: object,
    value_numeric: object,
    sample_date: object,
) -> str:
    """Stable digest identifying one lab *observation* within a source document.

    Persisted as :attr:`LabResult.dedup_key` and made unique per patient by
    ``uq_lab_results_observation``, so "the same observation from the same document twice" is
    refused by the database and not only by the read-then-insert check in ``GraphService``.
    See the index for why that matters.

    Every component is folded into one string before hashing rather than left as four columns of
    a composite unique index, because two of them are nullable and NULLs are distinct in a unique
    index on both dialects (PostgreSQL's ``NULLS NOT DISTINCT`` is 15+, and SQLite has no
    equivalent). The undated and non-numeric observations would be exactly the rows left
    unconstrained.

    The timestamp is reduced to a UTC ISO string rather than hashed as a ``datetime`` because the
    two sides of a comparison come from different places: one was just parsed out of an
    extraction (always tz-aware) and the other was read back out of the database, which on SQLite
    drops the tzinfo on the round trip.

    Arguments are typed ``object`` on purpose. This runs as a column default over the raw INSERT
    parameters, so it sees whatever a caller put there rather than what the ``Mapped`` annotation
    promises, and a key that raises is an insert that fails for a reason unrelated to dedup.
    Every component normalises defensively and falls back to text.
    """
    material = "\x1f".join(
        (
            "" if source_document_id is None else str(source_document_id),
            (marker_name or "").strip().lower() if isinstance(marker_name, str) else "",
            _canonical_number(value_numeric),
            _canonical_timestamp(sample_date),
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _derive_dedup_key(context: Any) -> str:
    """Column default: derive the key from the row being inserted.

    A default rather than something each caller passes, because a caller that forgets it is the
    precise failure the constraint exists to prevent — the row would go in unconstrained (or, with
    a NOT NULL and no default, break an unrelated insert path at runtime). Deriving it here means
    every writer of a lab result is covered by construction, including the ones in tests that
    stand rows up directly.
    """
    params = context.get_current_parameters()
    return lab_observation_key(
        params.get("source_document_id"),
        params.get("marker_name"),
        params.get("value_numeric"),
        params.get("sample_date"),
    )


class LabResult(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Lab test result. Supports both numeric and qualitative (text) values."""

    __tablename__ = "lab_results"
    __table_args__ = (
        CheckConstraint(
            "abnormality_direction IS NULL OR abnormality_direction IN "
            "('high', 'low', 'critical_high', 'critical_low')",
            name="ck_lab_results_abnormality_direction",
        ),
        # Serves the patient_id lookup for the longitudinal-record and lab-safety reads. Labs
        # accumulate per document ingested, so this is the set that grows fastest for a
        # long-running patient.
        #
        # The trailing sort columns were measured NOT to pay when 0008 landed: with 3000 labs
        # for one patient, PostgreSQL 16 bitmap-scanned on patient_id and sorted rather than
        # reading the index in order. That was the correct choice while RecordService.assemble
        # took the whole set -- with no LIMIT there is nothing to stop early on, and the bitmap
        # scan reads the heap sequentially and sorts 3000 rows in 445 kB of work_mem where an
        # ordered index scan would fetch every heap row in index order (random I/O). They were
        # kept for exactly the case the comment named: "if this read ever gains a LIMIT".
        #
        # It has. RecordService.assemble pages every section, so the ordering now has something
        # to stop early on. One caveat before trusting that: the read's ORDER BY ends in the
        # primary key -- a total order is what makes LIMIT/OFFSET a partition rather than a
        # lottery over ties -- and this index does not carry it, so the planner is choosing
        # between an incremental sort over the index prefix and the old bitmap-scan-and-sort.
        # Which it picks at production row counts has NOT been re-measured on PostgreSQL 16.
        #
        # The NULLS LAST is required either way: both readers sort NULLS LAST (sample_date is
        # nullable) and a DESC index in PostgreSQL is NULLS FIRST unless told otherwise.
        # SQLite's parser rejects NULLS LAST inside a CREATE INDEX at any version, hence the
        # PostgreSQL-only emission and the sibling below.
        Index(
            "ix_lab_results_patient_sample_date",
            "patient_id",
            text("sample_date DESC NULLS LAST"),
            "marker_name",
        ).ddl_if(dialect="postgresql"),
        # SQLite sibling of the above, minus the NULLS LAST its parser rejects. It exists so
        # that dropping the redundant single-column ix_lab_results_patient_id (migration 0009)
        # does not leave the dev/test database with patient_id unindexed altogether -- the
        # PostgreSQL-only index above is what covers that lookup in production.
        Index(
            "ix_lab_results_patient_sample_date",
            "patient_id",
            text("sample_date DESC"),
            "marker_name",
        ).ddl_if(dialect="sqlite"),
        # The durable half of the re-approval guard. GraphService deduplicates by reading the
        # keys already on the chart and skipping any it matches, which is a read-then-insert:
        # two approvals of one document that overlap in that window both read a chart without
        # the observation and both insert it, and the patient's chart shows one blood draw as
        # two -- which reads as two independent measurements agreeing, and doubles the derived
        # eGFR that the renal contraindication rules go on to read.
        #
        # DocumentService.approve takes a row lock to close that window, but SQLAlchemy's SQLite
        # dialect silently drops FOR UPDATE, so the lock is a PostgreSQL-only guarantee. This
        # index holds on every backend: the loser of the race fails its INSERT rather than
        # writing the duplicate, and approve() turns that into a 409 the client can retry into
        # the ordinary (deduplicating) sequential path.
        #
        # Partial on is_deleted so that a soft-deleted observation does not permanently forbid
        # re-merging the document it came from. Both dialects support partial indexes, and the
        # matching predicate is already on every dedup read.
        Index(
            "uq_lab_results_observation",
            "patient_id",
            "dedup_key",
            unique=True,
            sqlite_where=text("is_deleted = 0"),
            postgresql_where=text("is_deleted = false"),
        ),
    )

    # No single-column index: the composite above leads with patient_id on both dialects.
    # See migration 0009.
    patient_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("patients.id"), nullable=False)
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("encounters.id"), nullable=True
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=True
    )
    # sha256 hex of (source document, normalised marker, value, sample timestamp). Derived, never
    # supplied: see lab_observation_key and _derive_dedup_key above.
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False, default=_derive_dedup_key)
    marker_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    marker_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    value_numeric: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    value_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reference_range_low: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    reference_range_high: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    reference_range_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_abnormal: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    abnormality_direction: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sample_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reported_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lab_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extraction_region: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    extraction_confidence: Mapped[dict] = mapped_column(
        JSONBType, nullable=False, default=dict, server_default="{}"
    )
    clinician_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    clinician_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
