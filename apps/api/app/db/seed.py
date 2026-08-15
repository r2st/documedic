"""Reconciling seed loader for drug vocabulary, interactions, and contraindications.

Run as a module:  python -m app.db.seed

The curated JSON files under ``data/drugs`` are the source of truth for this reference data,
and this module makes a database match them. Each loader is keyed on a natural identifier
(reference id / interaction pair / drug+condition), so re-running never duplicates a row, and
each returns the number of rows it *changed* — zero on a database that is already in step.

**This used to insert and nothing else**, which made it idempotent in the only sense anybody
checked (a second run adds no rows) and inert in the sense that matters. A row whose key was
already present was skipped entirely, so every correction to the corpus stopped at the file:

* raising an interaction from ``major`` to ``contraindicated`` — a warning that should have
  become a hard block (Critical Safety Rule #3) — changed nothing on a deployed database;
* flipping a contraindication's ``is_absolute`` to true, likewise;
* adding the ``components`` list to a fixed-dose combination that was curated without one.
  ``data/drugs/schema.json`` says a combination carrying no component list "matches no
  interaction, no contraindication and no allergy, and returns a clean check for a drug
  nothing was checked against" — so this is the correction that removes a whole product's
  safety evaluation, and it was the one that could not land;
* adding a ``hepatotoxicity`` tier, or fixing a wrong ``drug_class`` that allergy
  cross-reactivity and duplicate-therapy both key on.

And the seeder reported ``0`` for all of it, which reads as "already up to date" in the
startup log rather than "your correction is not loaded". The only way to apply a curated fix
was to know that and drop the tables by hand.

Three further properties follow from reconciling rather than inserting:

* **Absent keys reset.** The desired row is built from the file with an explicit default for
  every curated column, not with ``.get`` over whatever keys happen to be present, so
  *removing* a wrong ``hepatotoxicity`` tier or a mistaken ``renal_threshold`` lands too.
* **Keys are compared the way the safety engine compares them** — case-folded reference ids
  and condition names (``app.core.safety._norm``). Re-curating "Peptic ulcer" as "Peptic
  Ulcer" is an edit to one rule, not a second rule; on the contraindications table, which
  carries no unique constraint, the insert-only loader would have made it a duplicate row.
  Pre-existing duplicates that collide under those keys are collapsed: the first row is
  reconciled and the rest deactivated, so two rows can no longer disagree about one drug.
* **Withdrawal lands.** A curated row dropped from the file is deactivated rather than left
  serving. For the vocabulary that means the drug stops resolving and reaches the clinician
  through ``check_unevaluated_medications`` as a visible gap, which is this project's standing
  preference over an answer from data the curator has retracted.

``source_version`` — three columns that existed on the models and were never written by
anything — now carries a short digest of the file each row was loaded from, so an operator can
ask a deployed database which revision of the corpus it is actually serving. Nothing reads it
for a clinical decision; it exists so "is the fix deployed?" has an answer that is not "diff
the rows by hand".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_sessionmaker
from app.models.drug_vocabulary import Contraindication, DrugInteraction, DrugVocabulary

# repo_root/data/drugs
DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "drugs"

# The columns each loader owns, with the value an absent key means. Everything here is written
# on every reconcile, so dropping a key from the file resets the column rather than leaving the
# previous value in place — see the module docstring.
_VOCABULARY_COLUMNS: dict[str, Any] = {
    "brand_name": None,
    "generic_name": None,
    "atc_code": None,
    "drug_class": None,
    "strength": None,
    "form": None,
    "manufacturer": None,
    "hepatotoxicity": None,
    "components": None,
}
_INTERACTION_COLUMNS: dict[str, Any] = {
    "severity": None,
    "interaction_type": None,
    "description": None,
    "clinical_effect": None,
    "management": None,
    "evidence_level": None,
    "source": "curated",
}
_CONTRAINDICATION_COLUMNS: dict[str, Any] = {
    "icd10_code": None,
    "severity": None,
    "description": None,
    "is_absolute": False,
    "renal_threshold": None,
    "hepatic_threshold": None,
    "source": "curated",
}


class EmptyCorpusError(RuntimeError):
    """A curated data file loaded to zero rows.

    Reconciling against it would deactivate every row of that table — the whole interaction
    corpus, or every contraindication — and a safety engine with nothing to match against
    returns a clean check for everything. A truncated file, a bad merge, or a half-written
    export is far likelier than a deliberate decision to withdraw an entire table, so this
    refuses rather than applies it. Withdrawing one rule still works: drop that entry.
    """


def _load(name: str) -> list[dict]:
    rows = json.loads((DATA_DIR / name).read_text())
    if not rows:
        raise EmptyCorpusError(
            f"{name} contains no entries; refusing to deactivate the table it seeds"
        )
    return rows


def corpus_version(name: str) -> str:
    """Short content digest of a curated data file, recorded as each row's ``source_version``.

    Content-addressed rather than a hand-maintained number, because a version a curator has to
    remember to bump is a version that disagrees with the file. Twelve hex characters is ample
    for telling two revisions apart by eye in a log line or a database column.
    """
    return hashlib.sha256((DATA_DIR / name).read_bytes()).hexdigest()[:12]


def _fold(value: str | None) -> str:
    """Case-fold and strip a curated key, matching how ``app.core.safety`` compares them."""
    return (value or "").strip().lower()


def _interaction_key(a: str | None, b: str | None) -> tuple[str, str]:
    """The unordered, case-folded identity of an interaction rule.

    Unordered because "warfarin with aspirin" and "aspirin with warfarin" are one rule; the
    engine looks the pair up both ways round.
    """
    left, right = _fold(a), _fold(b)
    return (left, right) if left <= right else (right, left)


def _apply(row: object, desired: dict[str, Any], version: str) -> bool:
    """Write ``desired`` and ``version`` onto ``row``; report whether the *content* changed.

    ``source_version`` is stamped either way but never counts, because it is a digest of the
    whole file: one corrected interaction would otherwise re-stamp all thirty-five rows and
    report thirty-five changes, drowning the one that matters in the startup log. The count is
    "rows whose curated content moved", which is the number an operator is reading it for.
    """
    changed = False
    for column, value in desired.items():
        if getattr(row, column) != value:
            setattr(row, column, value)
            changed = True
    if row.source_version != version:  # type: ignore[attr-defined]
        row.source_version = version  # type: ignore[attr-defined]
    return changed


def _desired(entry: dict, columns: dict[str, Any]) -> dict[str, Any]:
    """The full curated column set for one file entry, defaults filled in."""
    desired = {column: entry.get(column, default) for column, default in columns.items()}
    desired["is_active"] = True
    return desired


async def _reconcile(
    db: AsyncSession,
    *,
    model: type,
    existing: Sequence[Any],
    key_of: Any,
    entries: list[dict],
    columns: dict[str, Any],
    version: str,
    identity: Any,
) -> int:
    """Make ``existing`` rows match ``entries``, returning the number of rows changed.

    ``key_of`` maps a row (and, via ``identity``, a file entry) to the natural key the two are
    matched on. Rows sharing a key are collapsed onto the first: the duplicates are deactivated
    rather than deleted, because a row this loader did not create may be referenced elsewhere
    and because deactivation is reversible by re-curating.
    """
    by_key: dict[Any, list[Any]] = {}
    for row in existing:
        by_key.setdefault(key_of(row), []).append(row)

    changed = 0
    seen: set[Any] = set()
    for entry in entries:
        key = identity(entry)
        desired = _desired(entry, columns)
        rows = by_key.get(key, [])
        if key in seen:
            # The file itself names one rule twice. Reconciling the second occurrence would
            # silently let whichever entry came last win; skipping it keeps the loader's
            # behaviour first-wins, the same rule the resolver applies to colliding names.
            continue
        seen.add(key)
        if not rows:
            db.add(model(**desired, source_version=version))
            changed += 1
            continue
        if _apply(rows[0], desired, version):
            changed += 1
        for duplicate in rows[1:]:
            if duplicate.is_active:
                duplicate.is_active = False
                changed += 1

    for key, rows in by_key.items():
        if key in seen:
            continue
        for row in rows:
            if row.is_active:
                row.is_active = False
                changed += 1
    return changed


async def seed_drug_vocabulary(db: AsyncSession) -> int:
    """Reconcile ``drug_vocabulary`` against ``drug_vocabulary.json``. Returns rows changed."""
    entries = _load("drug_vocabulary.json")
    existing = (await db.execute(select(DrugVocabulary))).scalars().all()
    return await _reconcile(
        db,
        model=DrugVocabulary,
        existing=existing,
        key_of=lambda row: _fold(row.reference_id),
        entries=entries,
        columns={**_VOCABULARY_COLUMNS, "reference_id": None, "source": "curated"},
        version=corpus_version("drug_vocabulary.json"),
        identity=lambda entry: _fold(entry["reference_id"]),
    )


async def seed_interactions(db: AsyncSession) -> int:
    """Reconcile ``drug_interactions`` against ``interactions.json``. Returns rows changed."""
    entries = _load("interactions.json")
    existing = (await db.execute(select(DrugInteraction))).scalars().all()
    return await _reconcile(
        db,
        model=DrugInteraction,
        existing=existing,
        key_of=lambda row: _interaction_key(row.drug_a_reference_id, row.drug_b_reference_id),
        entries=entries,
        columns={**_INTERACTION_COLUMNS, "drug_a_reference_id": None, "drug_b_reference_id": None},
        version=corpus_version("interactions.json"),
        identity=lambda entry: _interaction_key(
            entry["drug_a_reference_id"], entry["drug_b_reference_id"]
        ),
    )


async def seed_contraindications(db: AsyncSession) -> int:
    """Reconcile ``contraindications`` against ``contraindications.json``. Returns rows changed."""
    entries = _load("contraindications.json")
    existing = (await db.execute(select(Contraindication))).scalars().all()
    return await _reconcile(
        db,
        model=Contraindication,
        existing=existing,
        key_of=lambda row: (_fold(row.drug_reference_id), _fold(row.condition_name)),
        entries=entries,
        columns={**_CONTRAINDICATION_COLUMNS, "drug_reference_id": None, "condition_name": None},
        version=corpus_version("contraindications.json"),
        identity=lambda entry: (
            _fold(entry["drug_reference_id"]),
            _fold(entry["condition_name"]),
        ),
    )


async def seed_guidelines(db: AsyncSession) -> int:
    # Local import keeps the optional RAG dependencies out of the core seed path.
    from app.services.guideline_ingest import ingest

    return await ingest(db)


async def seed_all(db: AsyncSession) -> dict[str, int]:
    counts = {
        "drug_vocabulary": await seed_drug_vocabulary(db),
        "drug_interactions": await seed_interactions(db),
        "contraindications": await seed_contraindications(db),
        "guideline_chunks": await seed_guidelines(db),
    }
    await db.commit()
    return counts


async def _main() -> None:  # pragma: no cover — `python -m app.db.seed` entrypoint
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        counts = await seed_all(db)
    print("Seeded:", counts)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_main())
