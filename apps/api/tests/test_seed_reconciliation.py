"""A correction to the curated safety corpus must reach an already-seeded database.

``app.db.seed`` was insert-only: a row whose natural key already existed was skipped, and the
loader returned 0 — the same value it returns when nothing needs doing. So on any deployment
that had booted once, editing ``data/drugs`` changed nothing and said so in a way that read as
"already up to date".

The three edits below are the ones that matter, and each of them is a hard block that does not
appear:

  * an interaction raised to ``contraindicated`` (Critical Safety Rule #3),
  * a contraindication flipped to ``is_absolute``,
  * a combination product given the ``components`` list without which — per
    ``data/drugs/schema.json`` — it "matches no interaction, no contraindication and no
    allergy, and returns a clean check for a drug nothing was checked against".

The rest of this file pins the properties reconciling brings with it: absent keys reset rather
than linger, the seeder's idea of "the same rule" matches the safety engine's (case-folded),
duplicates that could disagree are collapsed, a withdrawn row stops serving, an empty file is
refused rather than applied, and a second run over an unchanged corpus is still a no-op.
"""

from __future__ import annotations

import json
import shutil

import pytest
from sqlalchemy import func, select

from app.db import seed as seed_mod
from app.db.seed import (
    EmptyCorpusError,
    corpus_version,
    seed_contraindications,
    seed_drug_vocabulary,
    seed_interactions,
)
from app.models.drug_vocabulary import Contraindication, DrugInteraction, DrugVocabulary


@pytest.fixture
def curated(tmp_path, monkeypatch):
    """A writable copy of ``data/drugs`` that the seed module loads from.

    Returns a helper that rewrites one file and re-runs its loader, which is exactly the
    operational move under test: curate a fix, redeploy, and see whether it lands.
    """
    corpus = tmp_path / "drugs"
    shutil.copytree(seed_mod.DATA_DIR, corpus)
    monkeypatch.setattr(seed_mod, "DATA_DIR", corpus)

    class Corpus:
        path = corpus

        def read(self, name: str) -> list[dict]:
            return json.loads((corpus / name).read_text())

        def write(self, name: str, rows: list[dict]) -> None:
            (corpus / name).write_text(json.dumps(rows, indent=2))

        def edit(self, name: str, match, **changes) -> None:
            rows = self.read(name)
            hits = [row for row in rows if match(row)]
            assert hits, f"no {name} entry matched — the fixture data has moved"
            for row in hits:
                row.update(changes)
            self.write(name, rows)

    return Corpus()


async def _one(db, model, **filters):
    stmt = select(model)
    for column, value in filters.items():
        stmt = stmt.where(getattr(model, column) == value)
    return (await db.execute(stmt)).scalars().first()


# --- The corrections that could not land --------------------------------------------------


@pytest.mark.asyncio
async def test_raising_an_interaction_to_contraindicated_reaches_a_seeded_database(db, curated):
    """A warning re-curated as a hard block. Insert-only, the seeded row kept its old severity
    and the pair went on being dismissible."""
    pair = curated.read("interactions.json")[0]
    a, b = pair["drug_a_reference_id"], pair["drug_b_reference_id"]
    curated.edit(
        "interactions.json",
        lambda r: r["drug_a_reference_id"] == a and r["drug_b_reference_id"] == b,
        severity="contraindicated",
    )

    assert await seed_interactions(db) == 1
    await db.commit()

    row = await _one(db, DrugInteraction, drug_a_reference_id=a, drug_b_reference_id=b)
    assert row is not None
    assert row.severity == "contraindicated"


@pytest.mark.asyncio
async def test_flipping_a_contraindication_to_absolute_reaches_a_seeded_database(db, curated):
    """``is_absolute`` is the column that decides hard block vs. warning (Rule #3)."""
    entry = next(r for r in curated.read("contraindications.json") if not r["is_absolute"])
    drug, condition = entry["drug_reference_id"], entry["condition_name"]
    curated.edit(
        "contraindications.json",
        lambda r: r["drug_reference_id"] == drug and r["condition_name"] == condition,
        is_absolute=True,
        severity="absolute",
    )

    assert await seed_contraindications(db) == 1
    await db.commit()

    row = await _one(db, Contraindication, drug_reference_id=drug, condition_name=condition)
    assert row is not None and row.is_absolute is True


@pytest.mark.asyncio
async def test_adding_components_to_a_combination_reaches_a_seeded_database(db, curated):
    """The correction with the most at stake: without a component list a fixed-dose product
    matches no molecule-keyed rule at all and reports clean."""
    entry = next(r for r in curated.read("drug_vocabulary.json") if not r.get("components"))
    reference_id = entry["reference_id"]
    components = [
        {"reference_id": None, "generic_name": "Amoxicillin", "drug_class": "Penicillin"},
        {"reference_id": None, "generic_name": "Clavulanic acid", "drug_class": None},
    ]
    curated.edit(
        "drug_vocabulary.json",
        lambda r: r["reference_id"] == reference_id,
        components=components,
    )

    assert await seed_drug_vocabulary(db) == 1
    await db.commit()

    row = await _one(db, DrugVocabulary, reference_id=reference_id)
    assert row is not None and row.components == components


@pytest.mark.asyncio
async def test_a_hepatotoxicity_tier_added_after_first_boot_reaches_the_database(db, curated):
    entry = next(r for r in curated.read("drug_vocabulary.json") if not r.get("hepatotoxicity"))
    reference_id = entry["reference_id"]
    curated.edit(
        "drug_vocabulary.json",
        lambda r: r["reference_id"] == reference_id,
        hepatotoxicity="established",
    )

    assert await seed_drug_vocabulary(db) == 1
    await db.commit()

    row = await _one(db, DrugVocabulary, reference_id=reference_id)
    assert row is not None and row.hepatotoxicity == "established"


# --- Properties that follow from reconciling ----------------------------------------------


@pytest.mark.asyncio
async def test_removing_a_curated_key_resets_the_column(db, curated):
    """A wrong ``hepatotoxicity`` tier is corrected by *deleting* it. Building the desired row
    with ``.get(key, default)`` over every owned column — rather than only over the keys the
    file happens to carry — is what makes that land."""
    entry = next(r for r in curated.read("drug_vocabulary.json") if r.get("hepatotoxicity"))
    reference_id = entry["reference_id"]
    rows = curated.read("drug_vocabulary.json")
    for row in rows:
        if row["reference_id"] == reference_id:
            row.pop("hepatotoxicity")
    curated.write("drug_vocabulary.json", rows)

    assert await seed_drug_vocabulary(db) == 1
    await db.commit()

    stored = await _one(db, DrugVocabulary, reference_id=reference_id)
    assert stored is not None and stored.hepatotoxicity is None


@pytest.mark.asyncio
async def test_recasing_a_condition_name_edits_the_rule_instead_of_duplicating_it(db, curated):
    """``contraindications`` carries no unique constraint, and the safety engine matches
    condition names case-folded (``app.core.safety._norm``). An insert-only loader keyed on the
    raw string therefore turned "Peptic ulcer" -> "Peptic Ulcer" into a *second* row for the
    same rule, and two rows about one drug can disagree."""
    entry = curated.read("contraindications.json")[0]
    drug, condition = entry["drug_reference_id"], entry["condition_name"]
    before = await db.scalar(select(func.count()).select_from(Contraindication))

    curated.edit(
        "contraindications.json",
        lambda r: r["drug_reference_id"] == drug and r["condition_name"] == condition,
        condition_name=condition.upper(),
    )
    changed = await seed_contraindications(db)
    await db.commit()

    assert changed == 1
    assert await db.scalar(select(func.count()).select_from(Contraindication)) == before
    row = await _one(db, Contraindication, drug_reference_id=drug)
    assert row is not None and row.condition_name == condition.upper()


@pytest.mark.asyncio
async def test_an_interaction_pair_written_the_other_way_round_is_the_same_rule(db, curated):
    """Order and case are both presentation. Neither creates a second rule."""
    entry = curated.read("interactions.json")[0]
    a, b = entry["drug_a_reference_id"], entry["drug_b_reference_id"]
    before = await db.scalar(select(func.count()).select_from(DrugInteraction))

    curated.edit(
        "interactions.json",
        lambda r: r["drug_a_reference_id"] == a and r["drug_b_reference_id"] == b,
        drug_a_reference_id=b.lower(),
        drug_b_reference_id=a.lower(),
    )
    await seed_interactions(db)
    await db.commit()

    assert await db.scalar(select(func.count()).select_from(DrugInteraction)) == before


@pytest.mark.asyncio
async def test_duplicate_rows_that_could_disagree_are_collapsed_onto_one(db, curated):
    """Two rows for one rule is the state in which the answer depends on which row a query
    happened to return first. Reconciling keeps the first and deactivates the rest."""
    entry = curated.read("contraindications.json")[0]
    drug, condition = entry["drug_reference_id"], entry["condition_name"]
    db.add(
        Contraindication(
            drug_reference_id=drug,
            condition_name=condition.lower(),
            severity="relative",
            description="a second, disagreeing row for the same rule",
            is_absolute=False,
            source="curated",
        )
    )
    await db.flush()

    assert await seed_contraindications(db) >= 1
    await db.commit()

    live = (
        (
            await db.execute(
                select(Contraindication).where(
                    Contraindication.drug_reference_id == drug,
                    func.lower(Contraindication.condition_name) == condition.lower(),
                    Contraindication.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(live) == 1


@pytest.mark.asyncio
async def test_a_rule_withdrawn_from_the_corpus_stops_serving(db, curated):
    """Deactivated, not deleted: reversible by re-curating, and safe against a row something
    else references."""
    rows = curated.read("interactions.json")
    dropped = rows.pop()
    curated.write("interactions.json", rows)

    assert await seed_interactions(db) == 1
    await db.commit()

    row = await _one(
        db,
        DrugInteraction,
        drug_a_reference_id=dropped["drug_a_reference_id"],
        drug_b_reference_id=dropped["drug_b_reference_id"],
    )
    assert row is not None, "withdrawal must deactivate, not delete"
    assert row.is_active is False


@pytest.mark.asyncio
async def test_a_withdrawn_drug_is_deactivated_so_it_reaches_the_clinician_as_unevaluated(
    db, curated
):
    """A vocabulary row that stops resolving makes its medications *unevaluated* — a visible
    gap the clinician is told about — which this project prefers to an answer built from data
    the curator has retracted."""
    rows = curated.read("drug_vocabulary.json")
    dropped = rows.pop()
    curated.write("drug_vocabulary.json", rows)

    await seed_drug_vocabulary(db)
    await db.commit()

    row = await _one(db, DrugVocabulary, reference_id=dropped["reference_id"])
    assert row is not None and row.is_active is False


@pytest.mark.asyncio
async def test_a_drug_returned_to_the_corpus_is_reactivated(db, curated):
    """The other direction of the same rule — otherwise a withdrawal is permanent."""
    rows = curated.read("drug_vocabulary.json")
    dropped = rows.pop()
    curated.write("drug_vocabulary.json", rows)
    await seed_drug_vocabulary(db)
    await db.commit()

    curated.write("drug_vocabulary.json", [*rows, dropped])
    assert await seed_drug_vocabulary(db) == 1
    await db.commit()

    row = await _one(db, DrugVocabulary, reference_id=dropped["reference_id"])
    assert row is not None and row.is_active is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,loader",
    [
        ("drug_vocabulary.json", seed_drug_vocabulary),
        ("interactions.json", seed_interactions),
        ("contraindications.json", seed_contraindications),
    ],
)
async def test_an_empty_corpus_file_is_refused_rather_than_applied(db, curated, name, loader):
    """Reconciling against nothing would deactivate the whole table, and a safety engine with
    nothing to match against returns a clean check for everything. A truncated file or a bad
    merge is far likelier than a decision to withdraw an entire table at once."""
    curated.write(name, [])

    with pytest.raises(EmptyCorpusError):
        await loader(db)


@pytest.mark.asyncio
async def test_reconciling_an_unchanged_corpus_changes_nothing(db):
    """The property the old loader had and this must not lose: startup runs on every boot."""
    assert await seed_drug_vocabulary(db) == 0
    assert await seed_interactions(db) == 0
    assert await seed_contraindications(db) == 0


# --- Version tracking ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_seeded_row_records_which_corpus_revision_it_came_from(db):
    """``source_version`` existed on all three models and nothing ever wrote it, so there was
    no way to ask a deployed database which revision of the corpus it was serving."""
    for model, name in (
        (DrugVocabulary, "drug_vocabulary.json"),
        (DrugInteraction, "interactions.json"),
        (Contraindication, "contraindications.json"),
    ):
        version = corpus_version(name)
        rows = (await db.execute(select(model))).scalars().all()
        assert rows
        assert {row.source_version for row in rows} == {version}


@pytest.mark.asyncio
async def test_the_recorded_version_moves_when_the_corpus_changes(db, curated):
    """Content-addressed, so it cannot disagree with the file the way a hand-bumped number can."""
    before = corpus_version("interactions.json")
    curated.edit("interactions.json", lambda row: row is not None, evidence_level="probable")
    after = corpus_version("interactions.json")
    assert after != before

    await seed_interactions(db)
    await db.commit()

    rows = (await db.execute(select(DrugInteraction))).scalars().all()
    assert {row.source_version for row in rows} == {after}


@pytest.mark.asyncio
async def test_restamping_the_version_does_not_count_as_a_changed_row(db, curated):
    """The digest covers the whole file, so one corrected interaction moves every row's stamp.
    Counting those would report thirty-five changes for one edit and bury it in the log — the
    count means "rows whose curated content moved"."""
    rows = curated.read("interactions.json")
    curated.write("interactions.json", rows)  # reformatted, same content
    assert (
        corpus_version("interactions.json")
        != (await db.execute(select(DrugInteraction.source_version).limit(1))).scalar_one()
    )

    assert await seed_interactions(db) == 0
    await db.commit()

    stamps = (await db.execute(select(DrugInteraction.source_version))).scalars().all()
    assert set(stamps) == {corpus_version("interactions.json")}
