"""Account and Session models (auth domain)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.types import GUID, INETType
from app.models.base import (
    Base,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class Account(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Clinician/user account. Demo mode: no clinician-credential gate."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("email", name="uq_accounts_email"),)

    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    sessions: Mapped[list[Session]] = relationship(back_populates="account")


class Session(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Refresh-token session tracking. Raw tokens are never stored, only SHA-256 hashes."""

    __tablename__ = "sessions"

    account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Indexed for AuthService.purge_expired_sessions, whose whole job is to find rows past
    # this timestamp in a table that grows by one row per sign-in *and* per refresh.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ip_address: Mapped[str | None] = mapped_column(INETType(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Last time this refresh token was used to mint a new access token. Distinct from
    # updated_at (which also moves on is_revoked flips) — drives idle-session expiry.
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # When the *family* this row belongs to began — the password sign-in that started it.
    # Carried unchanged across every rotation, unlike created_at, so `expires_at` can be
    # anchored to the sign-in rather than to the newest token. Without it a client that kept
    # refreshing pushed its own absolute expiry forward forever and JWT_REFRESH_TTL_DAYS
    # bounded nothing; see migration 0020 and `AuthService._issue_tokens`.
    family_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # The last time the holder of this sign-in proved they know the password — the sign-in
    # itself, or a later `POST /auth/reauthenticate`. Read by the step-up gate in
    # `app.dependencies.require_recent_authentication`.
    #
    # A third timestamp on a row that already has three, because each answers a different
    # question and no two of them are substitutes:
    #   * `family_started_at` — when this sign-in began. Moves never.
    #   * `last_used_at` — when a token was last exchanged. Moves on every rotation, and so
    #     measures whether the *client* is alive, not whether a person is at it. A browser tab
    #     left open refreshes on a timer.
    #   * `last_authenticated_at` — when a *person* last typed the password. This one.
    # Carried across rotation exactly like `family_started_at`, because rotating a token is not
    # proof of anything about the person: if refresh re-stamped it, an idle tab would hold the
    # step-up gate open indefinitely and the control would be decorative.
    last_authenticated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    account: Mapped[Account] = relationship(back_populates="sessions")


class PasswordResetToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single-use, time-limited capability to set one account's password.

    Stored as a SHA-256 hash for the same reason refresh tokens are: a reader of this table
    must not come away able to sign in. The raw value exists only in the response (or log
    line) that delivered it and in the hands of whoever received it.

    ``used_at`` rather than a delete, so a replay is distinguishable from an expiry in the
    audit trail — a second presentation of a spent reset token is a signal, not a retry.
    """

    __tablename__ = "password_reset_tokens"

    account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Indexed for the same reason ``Session.expires_at`` is, and it was missed when
    # ``purge_spent_reset_tokens`` was written against the same shape: the sweep's only
    # predicate is ``expires_at < cutoff``, so without this it is a sequential scan of a table
    # that grows by a row per reset request — from an *unauthenticated* endpoint, which makes
    # it the one auth table an outsider can inflate. The sweep is not a background job either;
    # it runs inline on the sign-in path, so the scan lands in front of a clinician logging in.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Superseded by a later request for the same account, rather than used. Kept apart from
    # ``used_at`` so "someone requested three resets in a row" and "someone spent one" read
    # differently in the trail.
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_ip: Mapped[str | None] = mapped_column(INETType(), nullable=True)
