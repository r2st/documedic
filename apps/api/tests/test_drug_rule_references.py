"""A curated safety rule keyed on a drug the vocabulary does not carry.

Such a rule loads without complaint, is counted as applied, and can never fire.
``SafetyService.build_context`` scopes both rule tables to the drugs actually in play — the
reference ids come off the patient's *resolved* medications and the query is an exact ``IN`` —
so a rule whose ``drug_reference_id`` matches no vocabulary row is never fetched, never
evaluated, and reaches the clinician as a clean check rather than as an error.

Nothing detected it. Three ordinary ways to author one:

* a typo in a reference id;
* a difference in case. Ids are written uppercase (``ASP-75``), the engine compares exactly, and
  the seeder's own duplicate detection is case-*folded* — so it will treat ``asp-75`` and
  ``ASP-75`` as one rule while the engine treats them as two;
* withdrawing a drug from ``drug_vocabulary.json``, which deactivates its vocabulary row and
  leaves every rule keyed on it unreachable. Reconciling made that ordinary curation rather than
  a thing nobody does, which is why this is checked at load and not left to review.

``test_a_rule_the_vocabulary_cannot_resolve_silently_returns_a_clean_check`` is the one that
says why this matters: it plants exactly such a row and shows the hard block disappear.
"""

from __future__ import annotations

import json
import shutil

import pytest
from sqlalchemy import select

from app.db import seed as seed_mod
from app.db.seed import (
    DanglingRuleError,
    seed_contraindications,
    seed_drug_vocabulary,
    seed_interactions,
)
from app.models.drug_vocabulary import DrugInteraction
from tests.conftest import create_patient

# The one contraindicated pair in the shipped corpus — a nitrate with a PDE5 inhibitor. Used
# here because a hard block is the least ambiguous thing to watch vanish.
NITRATE = "ISMN-20"
PDE5 = "SIL-50"


@pytest.fixture
def curated(tmp_path, monkeypatch):
    """A writable copy of ``data/drugs`` that the seed module loads from."""
    corpus = tmp_path / "drugs"
    shutil.copytree(seed_mod.DATA_DIR, corpus)
    monkeypatch.setattr(seed_mod, "DATA_DIR", corpus)

    class Corpus:
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


async def _prescribe(client, patient_id: str, *brands: str) -> None:
    body = b"%PDF-1.4\nMEDICATIONS:\n" + b"".join(f"{b}\n".encode() for b in brands)
    upload = await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": ("rx.pdf", body, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    approve = await client.post(
        f"/api/v1/patients/{patient_id}/documents/{upload.json()['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approve.status_code == 200, approve.text


async def _check(client, patient_id: str, drug: str) -> dict:
    resp = await client.post(
        f"/api/v1/patients/{patient_id}/drug-safety/check", json={"drug_name": drug}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- What a dangling rule actually costs ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_rule_the_vocabulary_cannot_resolve_silently_returns_a_clean_check(auth_client, db):
    """The consequence, demonstrated rather than asserted about.

    A nitrate with a PDE5 inhibitor is the corpus's one contraindicated pair. Change nothing
    about the rule except the *case* of one endpoint — the severity, the description and the
    clinical fact are untouched — and the hard block is gone. Not downgraded to a warning, not
    reported as unevaluated: the check comes back clean, which is the answer a clinician acts
    on.
    """
    patient_id = (await create_patient(auth_client))["id"]
    await _prescribe(auth_client, patient_id, "Monotrate 20mg BD")

    blocked = await _check(auth_client, patient_id, "Sildenafil")
    assert blocked["is_blocked"] is True, "fixture check — this pair must hard-block to begin with"

    rule = (
        (
            await db.execute(
                select(DrugInteraction).where(DrugInteraction.drug_a_reference_id == NITRATE)
            )
        )
        .scalars()
        .first()
    )
    assert rule is not None and rule.drug_b_reference_id == PDE5
    rule.drug_a_reference_id = NITRATE.lower()
    await db.commit()

    after = await _check(auth_client, patient_id, "Sildenafil")
    assert after["is_blocked"] is False, (
        "this is the defect the load-time check exists to prevent, not a behaviour to keep"
    )


# --- The load-time refusal -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_shipped_corpus_resolves_end_to_end(db):
    """Every rule in ``data/drugs`` names a drug the vocabulary carries.

    The assertion that keeps a bad edit out of a release: the loaders raise on a dangling
    reference, so seeding the corpus as shipped is itself the check.
    """
    await seed_drug_vocabulary(db)
    await seed_interactions(db)
    await seed_contraindications(db)


@pytest.mark.asyncio
async def test_an_interaction_naming_an_unknown_drug_is_refused(db, curated):
    rows = curated.read("interactions.json")
    rows.append(
        {
            "drug_a_reference_id": "ASP-75",
            "drug_b_reference_id": "NO-SUCH-DRUG-1",
            "severity": "contraindicated",
            "description": "A rule for a drug that is not in the vocabulary.",
        }
    )
    curated.write("interactions.json", rows)

    with pytest.raises(DanglingRuleError) as raised:
        await seed_interactions(db)
    assert "NO-SUCH-DRUG-1" in str(raised.value)
    # The endpoint that *does* resolve is not reported as a problem.
    assert "ASP-75'" not in str(raised.value)


@pytest.mark.asyncio
async def test_a_contraindication_naming_an_unknown_drug_is_refused(db, curated):
    rows = curated.read("contraindications.json")
    rows.append(
        {
            "drug_reference_id": "NO-SUCH-DRUG-2",
            "condition_name": "Peptic ulcer",
            "is_absolute": True,
            "description": "A rule for a drug that is not in the vocabulary.",
        }
    )
    curated.write("contraindications.json", rows)

    with pytest.raises(DanglingRuleError) as raised:
        await seed_contraindications(db)
    assert "NO-SUCH-DRUG-2" in str(raised.value)


@pytest.mark.asyncio
async def test_a_case_only_mismatch_is_refused_and_named_as_one(db, curated):
    """The mismatch a curator cannot see by reading the two files side by side.

    ``asp-75`` is in the vocabulary as plainly as anything — just not in that case — so the
    message has to say the comparison is case-sensitive. "ASP-75 is not in the vocabulary" reads
    as a bug in the checker when the id is sitting there three lines down.
    """
    curated.edit(
        "interactions.json",
        lambda row: row["drug_a_reference_id"] == "ASP-75",
        drug_a_reference_id="asp-75",
    )

    with pytest.raises(DanglingRuleError) as raised:
        await seed_interactions(db)
    message = str(raised.value)
    assert "asp-75" in message
    assert "did you mean 'ASP-75'" in message
    assert "case-sensitive" in message


@pytest.mark.asyncio
async def test_withdrawing_a_drug_that_still_has_rules_is_refused(db, curated):
    """Ordinary curation, and the reason this is checked rather than reviewed.

    Reconciling deactivates a vocabulary row dropped from the file. Every interaction and
    contraindication keyed on that drug then becomes unreachable — so removing one line from one
    file silently retires however many safety rules pointed at it. Refusing turns that into a
    deploy that stops and names the rules that would have died.
    """
    curated.write(
        "drug_vocabulary.json",
        [row for row in curated.read("drug_vocabulary.json") if row["reference_id"] != PDE5],
    )

    with pytest.raises(DanglingRuleError) as raised:
        await seed_interactions(db)
    assert PDE5 in str(raised.value)


@pytest.mark.asyncio
async def test_a_rule_on_a_combination_s_molecule_resolves(db, curated):
    """A fixed-dose combination is evaluated as its ingredients, so a rule may name one.

    The identities a rule may be keyed on are products *and* the molecules inside combinations.
    Counting only the products would refuse the rules that make a combination checkable at all.
    """
    vocabulary = curated.read("drug_vocabulary.json")
    vocabulary.append(
        {
            "brand_name": "Testcombi",
            "generic_name": "Aspirin + Testolol",
            "reference_id": "TESTCOMBI-1",
            "components": [
                {"reference_id": "ASP-75", "generic_name": "Aspirin", "drug_class": "NSAID"},
                {
                    "reference_id": "TESTOLOL-1",
                    "generic_name": "Testolol",
                    "drug_class": "Beta blocker",
                },
            ],
        }
    )
    curated.write("drug_vocabulary.json", vocabulary)

    rules = curated.read("interactions.json")
    rules.append(
        {
            "drug_a_reference_id": "TESTOLOL-1",
            "drug_b_reference_id": "ASP-75",
            "severity": "major",
            "description": "A rule keyed on a molecule that only exists inside a combination.",
        }
    )
    curated.write("interactions.json", rules)

    await seed_drug_vocabulary(db)
    await seed_interactions(db)  # must not raise


@pytest.mark.asyncio
async def test_a_component_without_a_reference_id_is_not_treated_as_dangling(db, curated):
    """Clavulanic acid and friends: a molecule with no standalone vocabulary row.

    The model documents this as deliberate — such an ingredient still participates in allergy
    and duplicate-therapy matching and simply matches no reference-id-keyed rule. It is an
    absent key, not a broken one, and refusing it would refuse the shipped corpus.
    """
    vocabulary = curated.read("drug_vocabulary.json")
    vocabulary.append(
        {
            "brand_name": "Testmox CV",
            "generic_name": "Amoxicillin + Clavulanic acid",
            "reference_id": "TESTMOX-CV-1",
            "components": [
                {"generic_name": "Clavulanic acid", "drug_class": "Beta-lactamase inhibitor"}
            ],
        }
    )
    curated.write("drug_vocabulary.json", vocabulary)

    await seed_drug_vocabulary(db)
    await seed_interactions(db)  # must not raise
    await seed_contraindications(db)
