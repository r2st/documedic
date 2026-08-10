"""Idempotent seed loader for drug vocabulary, interactions, and contraindications.

Run as a module:  python -m app.db.seed
Keyed on natural identifiers (reference_id / interaction pair / drug+condition) so re-running
never duplicates rows.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_sessionmaker
from app.models.drug_vocabulary import Contraindication, DrugInteraction, DrugVocabulary

# repo_root/data/drugs
DATA_DIR = Path(__file__).resolve().parents[4] / "data" / "drugs"


def _load(name: str) -> list[dict]:
    return json.loads((DATA_DIR / name).read_text())


def _interaction_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


async def seed_drug_vocabulary(db: AsyncSession) -> int:
    existing = {r for (r,) in (await db.execute(select(DrugVocabulary.reference_id))).all()}
    added = 0
    for row in _load("drug_vocabulary.json"):
        if row["reference_id"] in existing:
            continue
        db.add(DrugVocabulary(source="curated", **row))
        existing.add(row["reference_id"])
        added += 1
    return added


async def seed_interactions(db: AsyncSession) -> int:
    existing = {
        _interaction_key(a, b)
        for (a, b) in (
            await db.execute(
                select(
                    DrugInteraction.drug_a_reference_id,
                    DrugInteraction.drug_b_reference_id,
                )
            )
        ).all()
    }
    added = 0
    for row in _load("interactions.json"):
        a, b = _interaction_key(row["drug_a_reference_id"], row["drug_b_reference_id"])
        if (a, b) in existing:
            continue
        payload = {**row, "drug_a_reference_id": a, "drug_b_reference_id": b}
        db.add(DrugInteraction(**payload))
        existing.add((a, b))
        added += 1
    return added


async def seed_contraindications(db: AsyncSession) -> int:
    existing = {
        tuple(row)
        for row in (
            await db.execute(
                select(Contraindication.drug_reference_id, Contraindication.condition_name)
            )
        ).all()
    }
    added = 0
    for row in _load("contraindications.json"):
        key = (row["drug_reference_id"], row["condition_name"])
        if key in existing:
            continue
        db.add(Contraindication(**row))
        existing.add(key)
        added += 1
    return added


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
