"""Add drug_vocabulary.components — the molecules a fixed-dose combination contains.

Every curated safety rule is keyed on a single molecule: an interaction is a pair of reference
ids, a contraindication is a reference id and a condition, an allergy match is a generic name or
a drug class. A combination product is one vocabulary row carrying several molecules, and
matching it against those rules by its own reference id, generic name and drug class matched it
against nothing at all — so Glycomet GP (metformin + glimepiride) at eGFR 20 returned no flags,
Augmentin returned no flags for a patient with a documented amoxicillin allergy, and Telma 40
beside Telma H was reported as two unrelated drugs rather than a doubled telmisartan dose.

NULL/empty means single-ingredient, which is the great majority of the vocabulary. The payload
is a list of ``{reference_id, generic_name, drug_class}`` — self-contained, so the safety engine
needs no second lookup, and ``reference_id`` may be null for a molecule with no standalone row
here (clavulanic acid, hydrochlorothiazide): such an ingredient still participates in allergy and
duplicate-therapy matching and simply matches no reference-id-keyed rule.

Curated, not derived by splitting the generic name on "+": which molecules a brand contains is a
fact about the product, and inferring it from punctuation would put an unverified ingredient list
underneath a hard block.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.migration_guards import column_exists

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    # 0001 builds the schema from the live ORM models, so a database created today already has
    # this column and a bare ADD COLUMN aborts the upgrade. See app.db.migration_guards.
    if column_exists(bind, "drug_vocabulary", "components"):
        return
    column_type = (
        postgresql.JSONB(astext_type=sa.Text()) if bind.dialect.name == "postgresql" else sa.JSON()
    )
    op.add_column("drug_vocabulary", sa.Column("components", column_type, nullable=True))


def downgrade() -> None:
    op.drop_column("drug_vocabulary", "components")
