"""CriticalLabAcknowledgement — a clinician recording that they have seen a panic value.

The gap this closes is that the critical-value screen had no reader. ``LabSafetyService`` has
detected panic values correctly for several rounds: it runs on every document approval, it
writes ``critical_lab_value_detected`` to the audit trail, and ``GET ../labs/critical-flags``
returns it on demand. All three of those require somebody to already be looking at that
patient. A potassium of 6.8 extracted from a report uploaded at 2am produced an audit entry
nobody reads and a flag on a chart nobody has open, and then waited.

So there are two halves here, and this table is the second:

* **The queue** (``LabSafetyService.outstanding_critical_values``) — every unacknowledged
  critical value across the whole panel, so the question "who on my list has a dangerous result
  right now" has an answer that does not require guessing which chart to open.
* **The acknowledgement** — a row here, which is what takes a finding *off* that queue.

Nothing else takes it off. There is deliberately no expiry, no auto-dismiss and no "seen"
inferred from having viewed the chart: a queue that empties itself is a queue that can be empty
because nobody looked. The only thing that clears an entry is a clinician saying so, and saying
so is a clinical act with their name on it.

**Append-only**, the same judgement ``clinical_suggestions`` and ``drug_safety_overrides`` are
held to (Critical Safety Rule #7). An acknowledgement is a clinician attesting that they saw a
result and what they did about it; a record of that which can be edited afterwards is not a
record of what was decided. A mistaken acknowledgement is corrected by acknowledging again —
the rows accumulate and the trail shows both.

**The value is copied onto the row, not only referenced.** ``lab_result_id`` says which result
this was, and ``value``/``unit``/``severity`` say what it said at the time it was acknowledged.
That is redundant until the day it is not: a lab row can be superseded by a corrected result
from the same document, and an acknowledgement that only pointed at the row would then read as
though the clinician had seen the corrected figure. What a clinician acknowledged is what was
in front of them.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID
from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class CriticalLabAcknowledgement(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One clinician's acknowledgement of one critical/panic lab value."""

    __tablename__ = "critical_lab_acknowledgements"
    __table_args__ = (
        # The queue's own predicate: "which of this account's critical values are already
        # acknowledged". Read on every queue request, against a table that grows by a row per
        # acknowledgement and is never pruned.
        Index(
            "ix_critical_lab_acks_account_lab",
            "account_id",
            "lab_result_id",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("accounts.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False, index=True
    )
    lab_result_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("lab_results.id"), nullable=False, index=True
    )

    # What was on the screen when this was acknowledged. See the module docstring: a corrected
    # result must not retroactively change what a clinician is recorded as having seen.
    marker_name: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(14, 4), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)

    # Who, by name, as typed. Not derived from ``account_id``: this product's deployment model
    # is one practice login held signed in across a shift and several machines
    # (``session_max_concurrent`` is 10 for exactly that reason), so the account says which
    # practice and not which person. An acknowledgement whose whole value is that a named
    # clinician takes responsibility for having seen a panic potassium cannot be signed by
    # "the practice".
    acknowledged_by: Mapped[str] = mapped_column(String(200), nullable=False)

    # What they did about it, in their own words. Optional, and deliberately so: the required
    # part is that a named clinician saw the value. Making the note mandatory would put a text
    # box between a clinician and clearing a queue at 2am, and the predictable result is a queue
    # cleared with "." rather than a queue cleared with a note. What is recorded here is
    # whatever they chose to write, and the audit payload records only its length.
    action_note: Mapped[str | None] = mapped_column(Text, nullable=True)
