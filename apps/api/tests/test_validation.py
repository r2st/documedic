"""Phase 4 — clinical validation, metrics, CDSCO dossier, safety reporting, pilot mode."""

from __future__ import annotations

import pytest

from tests.conftest import create_patient


@pytest.mark.asyncio
async def test_validation_run_computes_metrics(auth_client):
    resp = await auth_client.post("/api/v1/validation/run")
    assert resp.status_code == 201, resp.text
    run = resp.json()
    m = run["metrics"]
    assert run["vignette_count"] >= 5
    # Harness scored every dimension.
    for key in (
        "diagnostic_top1_accuracy",
        "diagnostic_top3_accuracy",
        "cant_miss_recall",
        "hard_block_accuracy",
        "autonomy_tier_distribution",
        "degraded_rate",
    ):
        assert key in m
    # The deterministic vignettes are designed so can't-miss recall and hard-block accuracy
    # are perfect (these are the safety-critical metrics).
    assert m["cant_miss_recall"] == 1.0
    assert m["hard_block_accuracy"] == 1.0
    assert m["diagnostic_top3_accuracy"] >= 0.8


@pytest.mark.asyncio
async def test_validation_run_listed_and_fetchable(auth_client):
    resp = await auth_client.post("/api/v1/validation/run")
    run_id = resp.json()["id"]
    resp = await auth_client.get("/api/v1/validation/runs")
    assert resp.status_code == 200
    assert any(r["id"] == run_id for r in resp.json())
    resp = await auth_client.get(f"/api/v1/validation/runs/{run_id}")
    assert resp.status_code == 200
    assert len(resp.json()["results"]) >= 5


@pytest.mark.asyncio
async def test_performance_metrics(auth_client):
    await auth_client.post("/api/v1/validation/run")
    resp = await auth_client.get("/api/v1/metrics/performance")
    assert resp.status_code == 200
    m = resp.json()
    assert m["total_sessions"] >= 5
    assert "autonomy_tier_distribution" in m
    assert m["citation_faithfulness_target"] == 0.95


@pytest.mark.asyncio
async def test_samd_dossier_json_and_markdown(auth_client):
    await auth_client.post("/api/v1/validation/run")
    resp = await auth_client.get("/api/v1/regulatory/samd-dossier")
    assert resp.status_code == 200
    d = resp.json()
    assert d["audit_integrity"]["chain_valid"] is True
    assert len(d["risk_management"]["controls"]) == 8
    assert d["clinical_validation"]["metrics"] is not None

    resp = await auth_client.get("/api/v1/regulatory/samd-dossier?format=markdown")
    assert resp.status_code == 200
    assert "CDSCO SaMD Technical Dossier" in resp.text
    assert "Risk Management Controls" in resp.text


@pytest.mark.asyncio
async def test_safety_report_filing(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        "/api/v1/safety-reports",
        json={
            "category": "incorrect_suggestion",
            "severity": "near_miss",
            "description": "Suggestion ranked a benign cause above a can't-miss diagnosis.",
            "patient_id": patient["id"],
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "open"

    resp = await auth_client.get("/api/v1/safety-reports")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # Open report shows up in performance metrics.
    resp = await auth_client.get("/api/v1/metrics/performance")
    assert resp.json()["open_safety_reports"] == 1


@pytest.mark.asyncio
async def test_safety_report_rejects_bad_severity(auth_client):
    resp = await auth_client.post(
        "/api/v1/safety-reports",
        json={"category": "x", "severity": "catastrophic", "description": "bad severity"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_pilot_status(auth_client):
    resp = await auth_client.get("/api/v1/pilot/status")
    assert resp.status_code == 200
    assert "pilot_mode" in resp.json()


@pytest.mark.asyncio
async def test_validation_harness_handles_a_vignette_with_no_intake_questions(
    auth_client, monkeypatch
):
    """The harness must not stall when triage asks nothing — it proceeds straight to reasoning."""
    from app.services.reasoning_service import ReasoningService

    async def _no_questions(self, session_id):
        return []

    submitted: list = []
    original_submit = ReasoningService.submit_answers

    async def _record_submit(self, account_id, session_id, answers):
        submitted.append(answers)
        return await original_submit(self, account_id, session_id, answers)

    monkeypatch.setattr(ReasoningService, "pending_questions", _no_questions)
    monkeypatch.setattr(ReasoningService, "submit_answers", _record_submit)

    resp = await auth_client.post("/api/v1/validation/run")
    assert resp.status_code == 201, resp.text
    run = resp.json()
    # Every vignette still ran and scored, and the intake loop broke out immediately.
    assert len(run["results"]) == run["vignette_count"] >= 5
    assert submitted == []
    # Safety-critical metrics are unaffected by the (absent) intake step.
    assert run["metrics"]["hard_block_accuracy"] == 1.0


@pytest.mark.asyncio
async def test_validation_run_is_scoped_to_the_owning_account(auth_client, client):
    """A validation run belongs to one account; another clinician cannot read it."""
    run_id = (await auth_client.post("/api/v1/validation/run")).json()["id"]

    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "other@example.com", "password": "password123", "display_name": "Dr Other"},
    )
    other = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    assert (await client.get(f"/api/v1/validation/runs/{run_id}", headers=other)).status_code == 404
    assert (await client.get("/api/v1/validation/runs", headers=other)).json() == []


@pytest.mark.asyncio
async def test_validation_run_missing_id_is_404(auth_client):
    import uuid

    resp = await auth_client.get(f"/api/v1/validation/runs/{uuid.uuid4()}")
    assert resp.status_code == 404
