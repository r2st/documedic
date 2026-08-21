"""A time-limited, revocable, read-only credential letting one patient see their own record.

What this is not
----------------
It is not an account. A patient does not sign up, does not choose a password, and has no
identity in ``accounts``; a clinician issues a credential against one chart and can withdraw it.
That shape is deliberate for a product whose tenancy model is "a chart belongs to one practice":
an account-based patient login would need a second identity system, a second recovery flow and a
second lockout policy, each of which is a way to reach a medical record.

The token is opaque, not a JWT
------------------------------
Stored only as a SHA-256 hash, exactly like ``sessions.token_hash`` and
``password_reset_tokens.token_hash``. Two reasons, and the second is the important one.

First, revocation must be immediate. A JWT is valid until it expires, so a signed portal token
would be a credential nobody could withdraw — and "my phone was stolen" is the single most
likely thing a patient will ever say to a practice about this feature.

Second, and structurally: a JWT would be a *third* token type in a vocabulary where
``app.dependencies.get_current_account`` accepts anything that decodes with the right ``type``
claim. Getting that wrong once would hand a patient's credential the clinician's API. An opaque
random string cannot be decoded at all, so the two authentication paths cannot be confused by
any amount of later editing — the portal dependency cannot accept a clinician's token and
``get_current_account`` cannot accept a patient's.

Expiry is mandatory
-------------------
``expires_at`` is NOT NULL. A read-only link to a medical record that never expires is a link
that outlives the reason it was issued, the device it was opened on, and eventually the clinical
relationship. The ceiling is enforced in the service.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PatientPortalGrant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One issued patient-portal credential."""

    __tablename__ = "patient_portal_grants"
    __table_args__ = (
        # A revocation is a moment and a reason, or it is not a revocation — the same pairing
        # ``encounter_participants`` is held to, for the same access-review reason.
        CheckConstraint(
            "(revoked_at IS NULL) = (revocation_reason IS NULL)",
            name="ck_patient_portal_grants_revocation_complete",
        ),
        # The authentication lookup: hash -> grant. Unique because two grants sharing a hash
        # would be two credentials nobody could tell apart, and the hash is of a random 64-byte
        # token, so a collision is a bug in the generator rather than a coincidence.
        Index("uq_patient_portal_grants_token", "token_hash", unique=True),
        # "Which credentials exist for this chart", which is the clinician's list and the
        # revocation UI's read.
        Index("ix_patient_portal_grants_patient", "patient_id", text("created_at DESC")),
    )

    patient_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("patients.id"), nullable=False)
    # The account that issued it. Kept for the audit trail: a portal read is attributed to the
    # chart and to the practice that opened the door, because the patient has no account id to
    # attribute it to.
    issued_by_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=False
    )
    # SHA-256 hex of the token. The token itself is returned once, at issue, and never stored:
    # a database dump must not be a set of working credentials to living patients' records.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Free text for the clinician's own list ("printed for Mrs Kumar at the desk"). Never shown
    # to the patient — it is the practice's note about the credential, not about the patient.
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Updated on each successful portal read. Not an access log — the audit trail is that — but
    # the one field that makes "has this link ever been used, and when last" answerable from
    # the clinician's list without a chain walk.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
