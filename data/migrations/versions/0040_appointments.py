"""The clinic diary: appointments and recorded provider hours.

Every artefact in this schema until now is retrospective — encounters record visits that
happened, labs record samples that were drawn, handovers record patients already handed over.
Nothing could say *when next*. A follow-up written into a consultation note as "review in six
weeks" is a follow-up nobody is listed for, and the two workflows this revision's round adds
around it — a protocol template's follow-up interval, and the patient's own view of their care
— both need somewhere to put that.

Two decisions worth not re-deriving.

**Overlap cannot be a constraint here, and the near-miss is worth stating.** PostgreSQL can
refuse overlapping bookings outright with ``EXCLUDE USING gist (provider WITH =, tstzrange(
starts_at, ends_at) WITH &&)``, which is exactly the right tool. It needs the ``btree_gist``
extension, and ``CREATE EXTENSION`` requires a superuser this deployment's migration role is
not — so a migration that reached for it would either fail the release or have to be wrapped in
a silent try/except, which is worse: the constraint would be absent on precisely the databases
nobody checked. What is installed instead is ``uq_appointments_provider_start``, which refuses a
*second live booking of the same provider at the same instant*. That is the shape a
double-submitted booking form takes, and it is the race the service's read-then-insert check
cannot close on its own. Partial overlap stays a service-level guarantee in
``app.core.scheduling.find_conflicts``; see the model docstring, which says the same thing to
the next person who reads the ORM rather than the migration.

**Working hours are stored in clinic-local time.** ``provider_availability`` holds a weekday and
two minute-offsets from local midnight, not UTC timestamps, because that is how a clinic states
its hours: "Tuesdays, nine to one" does not move when the offset does, and a row reading 03:30
would be unreadable to the person maintaining it. The single conversion happens in
``availability_verdict``, against ``CLINIC_UTC_OFFSET_MINUTES``.

A provider with no rows in that table has *unknown* hours, not no hours. The scheduler reports
that as its own answer rather than as "outside hours" or as silence — the same doctrine as the
safety engine's "not evaluated" flags, and the reason availability is a table rather than a
free-text note on a settings page.

No freeze trigger, unlike 0031's encounters and 0037's handoffs, and the absence is deliberate.
An appointment is a *plan*, and a plan that could not be moved would be useless; what must not
be rewritten is the record of what happened, which lives on the encounter the visit produces
and on the audit trail this revision's actions append to.

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_guards import index_exists
from app.models.appointment import CANCELLATION_COMPLETE, SLOT_IS_AN_INTERVAL
from app.core.scheduling import (
    APPOINTMENT_MODALITIES,
    APPOINTMENT_STATUSES,
    OCCUPYING_STATUSES,
)

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_APPOINTMENTS = "appointments"
_AVAILABILITY = "provider_availability"

# Taken from the model's own vocabularies rather than restated, so the rule this migration
# installs and the one ``create_all`` builds on a fresh database cannot drift into two rules.
_STATUS_LIST = ", ".join(f"'{s}'" for s in APPOINTMENT_STATUSES)
_MODALITY_LIST = ", ".join(f"'{m}'" for m in APPOINTMENT_MODALITIES)
_OCCUPYING_LIST = ", ".join(f"'{s}'" for s in OCCUPYING_STATUSES)

# (name, table, definition, predicate). Table names are written out literally rather than
# interpolated from the constants above so the AST reader in the test can see them — read back by
# ``tests/test_appointments.py::test_the_migration_indexes_match_the_orm`` so the ORM's
# declarations and this list cannot be edited apart.
_INDEXES: list[tuple[str, str, str, str]] = [
    (
        "uq_appointments_provider_start",
        "appointments",
        "(account_id, provider_name, starts_at)",
        f"status IN ({_OCCUPYING_LIST}) AND is_deleted = false",
    ),
    (
        "ix_appointments_provider_window",
        "appointments",
        "(account_id, provider_name, starts_at)",
        "is_deleted = false",
    ),
    (
        "ix_appointments_patient_start",
        "appointments",
        "(patient_id, starts_at DESC)",
        "is_deleted = false",
    ),
    (
        "ix_appointments_account_start",
        "appointments",
        "(account_id, starts_at)",
        "is_deleted = false",
    ),
    (
        "uq_provider_availability_slot",
        "provider_availability",
        "(account_id, provider_name, weekday, start_minute)",
        "is_deleted = false",
    ),
    (
        "ix_provider_availability_account_provider",
        "provider_availability",
        "(account_id, provider_name)",
        "is_deleted = false",
    ),
]

_UNIQUE = {"uq_appointments_provider_start", "uq_provider_availability_slot"}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 0001 builds the schema from live ORM metadata, so a database created today already carries
    # both tables and a bare create would abort the upgrade. Same guard as 0036/0037.
    inspector = sa.inspect(bind)
    if not inspector.has_table(_APPOINTMENTS):
        op.create_table(
            "appointments",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column(
                "patient_id",
                sa.Uuid(),
                sa.ForeignKey("patients.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("provider_name", sa.String(length=200), nullable=False),
            sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "status", sa.String(length=20), nullable=False, server_default="scheduled"
            ),
            sa.Column(
                "modality", sa.String(length=20), nullable=False, server_default="in_person"
            ),
            sa.Column("appointment_type", sa.String(length=50), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("source_protocol_key", sa.String(length=60), nullable=True),
            sa.Column(
                "booked_at_encounter_id",
                sa.Uuid(),
                sa.ForeignKey("encounters.id"),
                nullable=True,
            ),
            sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cancellation_reason", sa.Text(), nullable=True),
            sa.Column(
                "created_by_account_id",
                sa.Uuid(),
                sa.ForeignKey("accounts.id"),
                nullable=True,
            ),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.CheckConstraint(f"status IN ({_STATUS_LIST})", name="ck_appointments_status"),
            sa.CheckConstraint(
                f"modality IN ({_MODALITY_LIST})", name="ck_appointments_modality"
            ),
            sa.CheckConstraint(SLOT_IS_AN_INTERVAL, name="ck_appointments_slot_interval"),
            sa.CheckConstraint(
                CANCELLATION_COMPLETE, name="ck_appointments_cancellation_complete"
            ),
        )

    if not inspector.has_table(_AVAILABILITY):
        op.create_table(
            _AVAILABILITY,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("provider_name", sa.String(length=200), nullable=False),
            sa.Column("weekday", sa.Integer(), nullable=False),
            sa.Column("start_minute", sa.Integer(), nullable=False),
            sa.Column("end_minute", sa.Integer(), nullable=False),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_provider_availability_weekday"),
            sa.CheckConstraint(
                "start_minute >= 0 AND end_minute <= 1440 AND start_minute < end_minute",
                name="ck_provider_availability_window",
            ),
        )

    for name, table, definition, predicate in _INDEXES:
        if index_exists(bind, table, name):
            continue
        unique = "UNIQUE " if name in _UNIQUE else ""
        op.execute(f"CREATE {unique}INDEX {name} ON {table} {definition} WHERE {predicate}")


def downgrade() -> None:
    """Drops both tables, and with them every booking ever made.

    A downgrade to 0039 is a return to a schema with no concept of an appointment, so there is
    nothing to preserve the rows in. An operator rolling back across this on a database in
    clinical use should dump both tables first — nothing here is trigger-protected, so a plain
    SELECT and a plain DROP both work.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name, _table, _definition, _predicate in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute(f"DROP TABLE IF EXISTS {_AVAILABILITY}")
    op.execute(f"DROP TABLE IF EXISTS {_APPOINTMENTS}")
