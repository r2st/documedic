"""Statements the encounter schema makes about itself, checked without a database.

Two facts are written down twice in this feature, for reasons that are individually right and
jointly fragile. These pin them together. No fixtures, no dialect: they read the mapped table
and the module constants, so they run in the ordinary suite and fail at the moment the copies
diverge rather than at the moment the divergence costs something.
"""

from __future__ import annotations

from app.models.encounter import FROZEN_ON_SIGN, SIGNED_STATUSES, Encounter


def test_the_amendment_index_predicate_matches_the_signed_statuses():
    """The one place the lifecycle vocabulary is written twice, held together.

    ``uq_encounters_one_signed_amendment`` spells ``('signed', 'amended')`` literally instead of
    interpolating ``SIGNED_STATUSES``, because ``text()`` must take a literal string everywhere
    in this codebase (``test_sql_injection_surface``). That is the right trade, and it leaves a
    copy that can drift: a fifth status meaning "attested" would be added to the constant, the
    service would honour it, and the index would silently stop constraining amendments of it.
    """
    index = next(
        i for i in Encounter.__table__.indexes if i.name == "uq_encounters_one_signed_amendment"
    )
    predicates = " ".join(
        str(i.dialect_options[d]["where"]) for d, i in (("sqlite", index), ("postgresql", index))
    )
    for status in SIGNED_STATUSES:
        assert f"'{status}'" in predicates, (
            f"{status!r} counts as signed but the amendment index does not constrain it"
        )


def test_the_freeze_covers_every_clinical_column_on_the_encounter():
    """``FROZEN_ON_SIGN`` is what migration 0031 builds its trigger from, so a clinical column
    missing from it is a column a signed encounter can still be edited through.

    Enumerated from the mapped table rather than restated, so adding one to the model without
    deciding whether a signature covers it fails here instead of silently leaving a hole. The
    exempt set is the bookkeeping: the lifecycle's own columns, the timestamps, the soft-delete
    pair (chart withdrawal has to keep working) and the extraction provenance.
    """
    bookkeeping = {
        "id",
        "status",
        "amended_at",
        "created_at",
        "updated_at",
        "is_deleted",
        "deleted_at",
        "extraction_region",
        "extraction_confidence",
    }
    columns = {c.key for c in Encounter.__table__.columns}
    unfrozen = columns - set(FROZEN_ON_SIGN) - bookkeeping
    assert not unfrozen, (
        f"columns a signature does not cover: {sorted(unfrozen)}. Add each to FROZEN_ON_SIGN, "
        "or to the bookkeeping set here with a reason why a signed encounter may still change it"
    )
