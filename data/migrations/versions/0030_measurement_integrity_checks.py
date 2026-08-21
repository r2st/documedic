"""Integrity constraints on measurements and on the immutable suggestion record.

Three invariants that the application already maintains in one place each, made properties of
the schema so a second writer cannot reintroduce them.

1. ``lab_results`` / ``derived_markers`` reference intervals must not be inverted. A range whose
   low end is above its high end arrives from a misread — a two-column result sheet read in the
   wrong order, or the vision extractor emitting the two JSON fields swapped — and it makes
   every abnormality judgement on the row wrong in one direction: ``GraphService._merge_lab``
   tests the two bounds independently, so with the ends reversed every value inside the *true*
   reference range trips the ``value > high`` branch and is charted as abnormally high. That
   reaches the eight agents (``agents.tools.summarize_snapshot`` selects on ``is_abnormal`` to
   build the record summary they all reason from) and the FHIR export's observation
   interpretation. ``_merge_lab`` now drops both ends when it sees an inverted pair; this is the
   same rule at the schema.

2. ``derived_markers.value_numeric > 0``. Every marker in this table is a computed quantity that
   is strictly positive by construction, and the only one today is eGFR — the sole input to the
   metformin and renal-dose hard blocks. A zero is not a low result: it is arithmetic on a
   creatinine above roughly 4000 mg/dL, which is a misplaced decimal point rather than a
   reading, and it would hard-block off a number the formula never supported.

3. ``clinical_suggestions.confidence_band`` restricted to the ``ProbabilityBand`` vocabulary.
   It is the third of that table's three closed-vocabulary columns and was the only one with no
   constraint — and the only one written from a value the *model* supplied rather than chosen by
   our own code. The table is immutable by trigger, so a bad value written here can never be
   corrected in place.

Existing rows are repaired before each constraint is added: inverted intervals have both ends
set to NULL (the same "not evaluated" outcome the merge now produces, rather than a guess that
the numbers were right and only their order was wrong), and the two value/vocabulary checks are
added ``NOT VALID`` then validated, so a deployment carrying a row that violates them fails
loudly at the validate step with the row still present to look at rather than having it silently
rewritten.

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RANGE_ORDER = (
    "reference_range_low IS NULL OR reference_range_high IS NULL "
    "OR reference_range_low <= reference_range_high"
)
_BANDS = ("high", "moderate", "low", "very_low", "insufficient_data")
_CONFIDENCE_BAND = (
    "confidence_band IS NULL OR confidence_band IN (" + ", ".join(repr(b) for b in _BANDS) + ")"
)

# (table, constraint name, expression) for the two interval checks.
_RANGE_TABLES = (
    ("lab_results", "ck_lab_results_reference_range_order"),
    ("derived_markers", "ck_derived_markers_reference_range_order"),
)


# Every constraint here is declared on the model as well as added by this revision, and 0001
# builds the schema with ``Base.metadata.create_all`` — so on a *fresh* database all four already
# exist by the time this revision runs, while on a deployed one (stamped 0029, built before the
# models carried them) none do. A bare ADD CONSTRAINT is therefore right in production and a
# DuplicateObjectError on any new deployment.
#
# Dropping first makes the revision idempotent across both, which is what every earlier
# constraint migration here does implicitly by replacing rather than adding (0003, 0004, 0028,
# 0029 and the rest of the ``ck_dsc_check_type`` line). The drop is IF EXISTS because the
# production case has nothing to drop; the definition that lands is this revision's either way,
# so the two databases converge rather than depending on which path built them.
def _replace_constraint(table: str, name: str, ddl: str) -> None:
    """Add ``name`` to ``table``, whether or not create_all already put it there."""
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} {ddl}")


def upgrade() -> None:
    bind = op.get_bind()
    # SQLite (the test database) cannot ADD CONSTRAINT, and the model metadata carries every
    # check here, so a create_all-built database already has them. Same shape as 0028.
    if bind.dialect.name != "postgresql":
        return

    for table, name in _RANGE_TABLES:
        # Repair before constraining. Dropping both ends rather than swapping them: swapping
        # assumes the numbers are right and only their order is wrong, which is a guess about a
        # document nobody has re-read. With both ends NULL the row reads as "no reference range
        # recorded", which is what a lab with no printed range already looks like, and the
        # value itself is untouched.
        op.execute(
            f"UPDATE {table} SET reference_range_low = NULL, reference_range_high = NULL "
            "WHERE reference_range_low IS NOT NULL AND reference_range_high IS NOT NULL "
            "AND reference_range_low > reference_range_high"
        )
        _replace_constraint(table, name, f"CHECK ({_RANGE_ORDER})")

    # These two are added NOT VALID and then validated, so that a row already violating them
    # stops the migration with the row intact rather than being repaired by a guess. Neither has
    # a defensible automatic repair: a non-positive derived marker is a computation that should
    # never have been stored, and an unrecognised confidence band sits on a table that is
    # immutable by trigger.
    _replace_constraint(
        "derived_markers",
        "ck_derived_markers_value_positive",
        "CHECK (value_numeric > 0) NOT VALID",
    )
    op.execute("ALTER TABLE derived_markers VALIDATE CONSTRAINT ck_derived_markers_value_positive")
    _replace_constraint(
        "clinical_suggestions",
        "ck_clinical_suggestions_confidence_band",
        f"CHECK ({_CONFIDENCE_BAND}) NOT VALID",
    )
    op.execute(
        "ALTER TABLE clinical_suggestions "
        "VALIDATE CONSTRAINT ck_clinical_suggestions_confidence_band"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # The constraints come off; the NULLed reference ranges do not come back. That is stated
    # rather than attempted — the pre-migration values were an inverted pair that no longer
    # exists anywhere to restore from, and 0006's downgrade is this codebase's precedent for
    # refusing to pretend otherwise. Nothing clinical is lost by leaving them NULL: the lab
    # values themselves were never touched.
    op.drop_constraint(
        "ck_clinical_suggestions_confidence_band", "clinical_suggestions", type_="check"
    )
    op.drop_constraint("ck_derived_markers_value_positive", "derived_markers", type_="check")
    for table, name in _RANGE_TABLES:
        op.drop_constraint(name, table, type_="check")
