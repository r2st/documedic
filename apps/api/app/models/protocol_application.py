"""ProtocolApplication — the immutable record of one order set being applied to one chart.

Applying a template is the highest-leverage single action in this product: one request can
chart several medications, list a set of investigations and put a booking in the diary. What
that leaves behind on the chart afterwards is a handful of ordinary medication events and an
appointment, indistinguishable from ones typed in individually — and the question a later
reader asks is exactly the one those rows cannot answer: *were these five things one decision?*

So the application itself is a row. It says which template, which version of it, which items
the clinician kept, what was charted as a result, and who applied it.

**The version is stored, and that is the point of storing it.** ``app.core.order_sets.VERSION``
moves whenever the curated content changes. Without it on the row, a template corrected next
month would silently rewrite the history of every application made before the correction: the
record would say a clinician applied today's content. Same reasoning as the guideline corpus
version on a citation, and as ``extraction_prompt_version`` on a document.

**Append-only** (``CreatedAtMixin``, no ``updated_at``, no soft-delete pair), like
``clinical_suggestions``, ``drug_safety_overrides`` and ``critical_lab_acknowledgements``
(Critical Safety Rule #7). Undoing an application means stopping the medications and cancelling
the appointment, each of which is its own recorded act; it does not mean deleting the statement
that somebody applied it.

**Selection is stored, not inferred.** ``selected_keys`` is what the clinician actually kept,
which is not recoverable from the template plus the charted rows: an investigation that was
deselected leaves no trace, and a medication the patient was already on may have been kept and
then not charted twice. What a clinician chose *not* to order is a clinical decision, and this
is the only place it is written down.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class ProtocolApplication(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One application of one curated order set to one patient."""

    __tablename__ = "protocol_applications"
    __table_args__ = (
        # The chart's own list: WHERE patient_id = ? ORDER BY created_at DESC.
        Index(
            "ix_protocol_applications_patient_created",
            "patient_id",
            text("created_at DESC"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("accounts.id"), nullable=False)
    # No index of its own: ``ix_protocol_applications_patient_created`` leads with this column, so
    # a bare patient_id index would be its prefix — a second write on every insert buying nothing.
    patient_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("patients.id"), nullable=False)
    # The visit this was applied at, when it was applied during one.
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("encounters.id"), nullable=True
    )

    template_key: Mapped[str] = mapped_column(String(60), nullable=False)
    # See the module docstring: without this a curated correction rewrites history.
    template_version: Mapped[str] = mapped_column(String(20), nullable=False)
    # Copied rather than looked up on read, for the same reason the version is stored: the
    # title a clinician saw is part of what they applied, and a renamed template must not
    # relabel past applications.
    template_title: Mapped[str] = mapped_column(String(200), nullable=False)

    # What the clinician kept. See the module docstring for why this is not inferable.
    selected_keys: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    # The investigations the application recorded as ordered.
    #
    # This is a *list of orders*, not results, and it lives here rather than in ``lab_results``
    # deliberately: that table holds observations, and writing a row into it with no value would
    # put an investigation on the chart that reads as a test that came back empty. This product
    # has no order-entry integration to send a request to; what it can honestly record is that
    # a clinician decided to order these, which is what this column is.
    ordered_investigations: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    # The ``medication_events`` rows this application created, as strings. A list rather than a
    # join table: it is written once, read whole, and never queried across applications.
    medication_event_ids: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)

    follow_up_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("appointments.id"), nullable=True
    )

    # Who applied it, by name as typed — the same reasoning as the handover's two clinicians
    # and the critical-lab acknowledgement: the account identifies the practice, not the person.
    applied_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # How many non-blocking safety findings stood against the medications at the moment of
    # application. A hard block refuses the application outright, so this counts what the
    # clinician proceeded *past*: the number is the record that there was something to read.
    warning_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
