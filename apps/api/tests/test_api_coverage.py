"""API-flow branches the suite never reached: clinician corrections, startup seeding, exports.

These are the remaining lines the full-suite coverage report listed as missing in
``app/main.py``, ``app/services/document_service.py``, ``app/services/regulatory_service.py``
and the extraction pipeline's demo net. Unlike the pure-unit gaps, each of these only happens
partway through a real request or process lifecycle, so they are driven end-to-end here.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.models.user import Account
from app.services.document_service import DocumentService
from app.services.extraction.pipeline import ExtractionPipeline
from app.services.extraction.text_parser import ParsedEntity
from app.services.regulatory_service import RegulatoryService
from tests.conftest import create_patient

PRESCRIPTION = (
    b"%PDF-1.4\nMEDICATIONS:\nGlycomet 500mg BD\nLABS:\nCreatinine: 3.0 mg/dL (0.6-1.2)\n"
)


async def _upload(client, patient_id, content=PRESCRIPTION, name="rx.pdf"):
    resp = await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, content, "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _account_id(db) -> uuid.UUID:
    account_id = (await db.execute(select(Account.id))).scalars().first()
    assert account_id is not None
    return account_id


# --------------------------------------------------------------- clinician corrections


async def test_a_clinician_correction_is_applied_promoted_to_high_confidence_and_audited(
    auth_client,
):
    """A corrected field is clinician-attested, so it must merge as high-confidence and be logged.

    Three things matter here. The corrected brand name has to reach the patient graph instead of
    the OCR value; it has to be resolved through the DrugVocabulary to its INN generic
    (CLAUDE.md pitfall #4) rather than stored as the raw string; and the correction has to leave
    an audit record — a silently-accepted edit to clinical data is what the trail exists for.
    """
    patient = await create_patient(auth_client)
    doc = await _upload(auth_client, patient["id"])

    extraction = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/extraction"
    )
    entities = extraction.json()["entities"]
    med_index = next(i for i, e in enumerate(entities) if e["entity_type"] == "medication")

    approved = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
        json={
            "corrections": [
                {"entity_index": med_index, "field_name": "brand_name_raw", "value": "Crocin"}
            ],
            "rejected_entity_indexes": [],
        },
    )
    assert approved.status_code == 200, approved.text

    # The correction reached the graph.
    record = await auth_client.get(f"/api/v1/patients/{patient['id']}/record")
    assert record.status_code == 200, record.text
    generics = [m["generic_name"] for m in record.json()["medications"]]
    assert "Paracetamol" in generics, f"the corrected brand did not resolve: {generics}"

    # ...and the stored extraction now shows the field as clinician-confirmed.
    after = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/extraction"
    )
    field = next(
        f for f in after.json()["entities"][med_index]["fields"] if f["name"] == "brand_name_raw"
    )
    assert field["value"] == "Crocin"
    assert field["confidence"] == 1.0
    assert field["confidence_band"] == "high"
    assert field["needs_confirmation"] is False

    # ...and it was audited.
    audit = await auth_client.get(f"/api/v1/patients/{patient['id']}/audit", params={"limit": 200})
    assert audit.status_code == 200, audit.text
    actions = [entry["action"] for entry in audit.json()["items"]]
    assert "field_corrected" in actions


async def test_a_correction_naming_an_out_of_range_entity_is_ignored(db, auth_client):
    """An index past the end of the extraction must be dropped, not raise or corrupt a field.

    A stale UI (extraction re-run since the page loaded) is the realistic source of this.
    """
    patient = await create_patient(auth_client)
    doc = await _upload(auth_client, patient["id"])
    doc_id = uuid.UUID(doc["id"])
    account_id = await _account_id(db)

    from app.schemas.document import ExtractionApproval, FieldCorrection

    counts = await DocumentService(db).approve(
        account_id=account_id,
        patient_id=uuid.UUID(patient["id"]),
        doc_id=doc_id,
        approval=ExtractionApproval(
            corrections=[
                FieldCorrection(entity_index=999, field_name="brand_name_raw", value="Nonsense"),
                FieldCorrection(entity_index=-1, field_name="brand_name_raw", value="Nonsense"),
            ]
        ),
    )

    assert isinstance(counts, dict)
    stored = await DocumentService(db).get(account_id, uuid.UUID(patient["id"]), doc_id)
    values = [
        f["value"]
        for ent in (stored.extraction_metadata or {}).get("entities", [])
        for f in ent["fields"]
    ]
    assert "Nonsense" not in values


async def test_a_correction_naming_an_unknown_field_leaves_the_entity_untouched(db, auth_client):
    patient = await create_patient(auth_client)
    doc = await _upload(auth_client, patient["id"])
    account_id = await _account_id(db)

    from app.schemas.document import ExtractionApproval, FieldCorrection

    await DocumentService(db).approve(
        account_id=account_id,
        patient_id=uuid.UUID(patient["id"]),
        doc_id=uuid.UUID(doc["id"]),
        approval=ExtractionApproval(
            corrections=[
                FieldCorrection(entity_index=0, field_name="no_such_field", value="Nonsense")
            ]
        ),
    )

    stored = await DocumentService(db).get(
        account_id, uuid.UUID(patient["id"]), uuid.UUID(doc["id"])
    )
    names = [
        f["name"]
        for ent in (stored.extraction_metadata or {}).get("entities", [])
        for f in ent["fields"]
    ]
    assert "no_such_field" not in names


# --------------------------------------------------------------- extraction demo net


async def test_illegible_bytes_fall_back_to_simulated_entities_in_demo_mode(monkeypatch):
    """An unreadable scan must still produce something to show when the demo net is on."""
    from app.services.extraction import claude_client

    monkeypatch.setattr(claude_client, "demo_active", lambda: True)
    monkeypatch.setattr(
        claude_client,
        "demo_extract",
        lambda: ([ParsedEntity(entity_type="medication", fields=[])], "prescription"),
    )
    monkeypatch.setattr(claude_client, "demo_model_label", lambda: "[DEMO MODE] simulated")

    result = ExtractionPipeline().run(b"%PDF-1.4\n\n", "pdf")

    assert result.document_type == "prescription"
    assert result.model == "[DEMO MODE] simulated"
    assert len(result.entities) == 1


async def test_illegible_bytes_yield_no_entities_when_the_demo_net_is_off(monkeypatch):
    """With the demo net off the pipeline must report nothing rather than invent content."""
    from app.services.extraction import claude_client

    monkeypatch.setattr(claude_client, "demo_active", lambda: False)

    result = ExtractionPipeline().run(b"%PDF-1.4\n\n", "pdf")

    assert result.entities == []
    assert result.document_type is None
    assert result.model is None


async def test_legible_text_that_parses_to_nothing_falls_back_to_the_demo_net(monkeypatch):
    """Text extracted but unparseable is the other demo-net entry point."""
    from app.services.extraction import claude_client

    monkeypatch.setattr(claude_client, "demo_active", lambda: True)
    monkeypatch.setattr(
        claude_client,
        "demo_extract",
        lambda: ([ParsedEntity(entity_type="condition", fields=[])], "lab_report"),
    )
    monkeypatch.setattr(claude_client, "demo_model_label", lambda: "[DEMO MODE] simulated")

    # Prose with no recognisable section headers or med/lab lines.
    result = ExtractionPipeline().run(
        b"%PDF-1.4\nthe quick brown fox jumped over nothing clinical\n", "pdf"
    )

    assert result.document_type == "lab_report"
    assert [e.entity_type for e in result.entities] == ["condition"]


# --------------------------------------------------------------- regulatory dossier


async def test_the_dossier_says_so_explicitly_when_no_validation_run_exists(db, auth_client):
    """An empty validation section must state that plainly, not render a blank metrics block.

    A regulator reading a silent gap cannot distinguish "not run" from "ran with no findings".
    """
    account_id = await _account_id(db)

    markdown = await RegulatoryService(db).render_markdown(account_id)

    assert "No validation run on record yet" in markdown
    assert "POST /validation/run" in markdown


async def test_the_dossier_reports_metrics_once_a_validation_run_has_happened(auth_client):
    run = await auth_client.post("/api/v1/validation/run")
    assert run.status_code in (200, 201), run.text

    resp = await auth_client.get("/api/v1/regulatory/samd-dossier", params={"format": "markdown"})

    assert resp.status_code == 200, resp.text
    markdown = resp.text
    assert "No validation run on record yet" not in markdown
    assert "## 4. Clinical Validation" in markdown


# --------------------------------------------------------------- startup seeding


async def test_startup_seeding_commits_reference_drug_data_and_logs_what_it_added(
    monkeypatch, caplog
):
    """First boot must load the deterministic safety tables; they gate every hard block."""
    from app import main as main_mod

    committed: list[bool] = []

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            committed.append(True)

        async def rollback(self):  # pragma: no cover — asserted not to run
            raise AssertionError("rollback must not run on a successful seed")

    monkeypatch.setattr(main_mod, "get_sessionmaker", lambda: _FakeDB)
    import app.db.seed as seed_mod

    monkeypatch.setattr(seed_mod, "seed_drug_vocabulary", lambda _db: _async(12))
    monkeypatch.setattr(seed_mod, "seed_interactions", lambda _db: _async(3))
    monkeypatch.setattr(seed_mod, "seed_contraindications", lambda _db: _async(4))

    with caplog.at_level(logging.INFO, logger="app.main"):
        await main_mod._seed_drug_data()

    assert committed == [True]
    assert any("Drug data seeded on startup" in r.message % r.args for r in caplog.records)


async def test_startup_seeding_is_quiet_when_the_reference_data_is_already_present(
    monkeypatch, caplog
):
    """A restart must not log a spurious "seeded" line when every table was already full."""
    from app import main as main_mod

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    monkeypatch.setattr(main_mod, "get_sessionmaker", lambda: _FakeDB)
    import app.db.seed as seed_mod

    monkeypatch.setattr(seed_mod, "seed_drug_vocabulary", lambda _db: _async(0))
    monkeypatch.setattr(seed_mod, "seed_interactions", lambda _db: _async(0))
    monkeypatch.setattr(seed_mod, "seed_contraindications", lambda _db: _async(0))

    with caplog.at_level(logging.INFO, logger="app.main"):
        await main_mod._seed_drug_data()

    assert not any("Drug data seeded on startup" in r.getMessage() for r in caplog.records)


async def test_startup_seeding_failure_rolls_back_and_warns_without_crashing_the_app(
    monkeypatch, caplog
):
    """A seeding failure must degrade to a warning — the app still serves record access."""
    from app import main as main_mod

    rolled_back: list[bool] = []

    class _FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):  # pragma: no cover — the seed raises first
            raise AssertionError("commit must not run after a seed failure")

        async def rollback(self):
            rolled_back.append(True)

    monkeypatch.setattr(main_mod, "get_sessionmaker", lambda: _FakeDB)
    import app.db.seed as seed_mod

    async def _boom(_db):
        raise RuntimeError("seed file missing")

    monkeypatch.setattr(seed_mod, "seed_drug_vocabulary", _boom)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await main_mod._seed_drug_data()  # must not raise

    assert rolled_back == [True]
    assert any("Drug data seeding failed on startup" in r.getMessage() for r in caplog.records)


async def _async(value):
    """Coroutine returning ``value`` — lets a lambda stand in for an async seed function."""
    return value
