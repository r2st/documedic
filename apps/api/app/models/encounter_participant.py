"""Who took part in one consultation, besides the account that owns the chart.

Two things this table does, and they are worth keeping distinct because only one of them is
about permissions.

**It records who was there.** ``encounters`` has a single ``signed_by_account_id``, so a note
written by a registrar under supervision and a note written by a consultant alone are the same
row on the record. Who wrote it, who supervised, who was asked for an opinion and who was
sitting in are clinical facts about the visit; a row here is where each of them is written down.

**It grants access to that one encounter.** A chart belongs to exactly one account, and before
this there was no setting between "the whole panel" and "nothing" — which in practice meant a
consultation was shared by email or read out over the phone. A participant reaches this
encounter and nothing else: not the chart, not the labs, not the other visits.

Append-with-a-tombstone, not delete
-----------------------------------
Removing a participant sets ``removed_at``; it never deletes the row. Access follows
``removed_at IS NULL`` and stops immediately, and what remains is the statement that this person
had access between these two times — which is the only form of the answer an access review can
use. A deleted row answers "who can see this now" and destroys "who could see it in March".

The unique index is therefore partial. One *live* participation per account per encounter, with
any number of historical ones behind it: a colleague brought in, removed, and brought back a
month later is two spells, and collapsing them would lose the gap.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encounter_roles import ENCOUNTER_ROLES
from app.db.types import GUID
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

_ROLE_VALUES = ", ".join(repr(role) for role in ENCOUNTER_ROLES)


class EncounterParticipant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One account's participation in one encounter, in one role."""

    __tablename__ = "encounter_participants"
    __table_args__ = (
        # The role vocabulary, interpolated from ``app.core.encounter_roles`` so the constraint
        # and the capability table cannot drift. A role in the column that the capability table
        # does not hold would be a participation nothing can decide the permissions of.
        CheckConstraint(f"role IN ({_ROLE_VALUES})", name="ck_encounter_participants_role"),
        # A removal is a moment and a reason, or it is not a removal. Both together, because a
        # withdrawn access with no recorded reason is the half of an access review that always
        # turns out to be the interesting half.
        CheckConstraint(
            "(removed_at IS NULL) = (removal_reason IS NULL)",
            name="ck_encounter_participants_removal_complete",
        ),
        # One live participation per account per encounter. Partial, so the historical spells
        # behind it are unconstrained — see the module docstring.
        #
        # This is the durable half of the "already a participant" check in the service, which is
        # a read-then-insert: two grants of the same colleague arriving together would both read
        # an encounter without them and both insert, leaving two live rows that a later removal
        # would revoke only one of. The loser of the race fails its INSERT instead.
        Index(
            "uq_encounter_participants_live",
            "encounter_id",
            "account_id",
            unique=True,
            sqlite_where=text("removed_at IS NULL"),
            postgresql_where=text("removed_at IS NULL"),
        ),
        # The two reads this table has. The first is "who is on this encounter", which every
        # authorization check on a shared route performs; the second is "which encounters have
        # been shared with me", which is the participant's only way in, since they cannot reach
        # the chart the encounter hangs off.
        Index("ix_encounter_participants_encounter", "encounter_id"),
        Index("ix_encounter_participants_account", "account_id", text("created_at DESC")),
    )

    encounter_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("encounters.id"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("accounts.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    # Who granted it. Always the account that owns the chart — nobody else may add a participant
    # — and stored rather than inferred, because ownership of a chart is a mutable fact and an
    # access review asks who granted this *at the time*.
    granted_by_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=False
    )
    # Free text, required of every grant, for the same reason ``DrugSafetyOverride.reasoning``
    # is: an access grant to a clinical record with no stated purpose is the row that cannot be
    # justified afterwards. DPDP purpose limitation, made a column rather than a policy.
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
