"""DEMO MODE — simulated LLM fallback when no provider is configured/reachable.

These tests opt INTO the demo net (the default suite runs deterministic offline mode; see
conftest). They verify that with no API keys the engine still produces realistic, clearly-marked
clinical output, that extraction returns sample data, and that health reports demo mode.
"""

from __future__ import annotations

import pytest

from app.agents import demo_data
from app.agents.llm import (
    LLMClient,
    LLMUnavailable,
    available_providers,
    demo_fallback_enabled,
    is_available,
    using_simulated_llm,
)
from app.agents.prompts import (
    CANT_MISS_SENTINEL,
    DEVILS_ADVOCATE,
    GUIDELINE_RAG,
    HYPOTHESIS_PANEL,
    INVESTIGATION_STRATEGIST,
    TRIAGE_INTAKE,
    VERIFIER,
)
from app.config import settings
from app.services.extraction.pipeline import ExtractionPipeline
from tests.conftest import create_patient


@pytest.fixture
def demo_llm(monkeypatch):
    """Force the simulated demo net: no provider keys, demo fallback enabled."""
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "llm_demo_fallback", True)
    assert not available_providers()
    assert using_simulated_llm()
    return settings


# --------------------------------------------------------------------- unit: llm client


def test_is_available_true_in_demo_mode(demo_llm):
    # Even with zero provider keys, the engine reports available via the demo net.
    assert is_available()


def test_complete_json_returns_simulated_payload_when_no_provider(demo_llm):
    client = LLMClient()
    result = client.complete_json(
        HYPOTHESIS_PANEL.replace("{specialty}", "Cardiology"),
        "Presenting complaint: central chest pain radiating to the left arm",
    )
    assert result["_demo"] is True
    assert result["hypotheses"], "demo hypotheses should be present"
    names = " ".join(h["diagnosis_name"].lower() for h in result["hypotheses"])
    assert "angina" in names or "coronary" in names


def test_complete_json_raises_when_demo_disabled(monkeypatch):
    from app.agents.llm import LLMUnavailable

    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "llm_demo_fallback", False)
    with pytest.raises(LLMUnavailable):
        LLMClient().complete_json(TRIAGE_INTAKE, "Presenting complaint: fever")


@pytest.mark.parametrize(
    "system,key",
    [
        (TRIAGE_INTAKE, "questions"),
        (HYPOTHESIS_PANEL.replace("{specialty}", "Infectious Disease"), "hypotheses"),
        (CANT_MISS_SENTINEL, "cant_miss"),
        (DEVILS_ADVOCATE, "summary"),
        (INVESTIGATION_STRATEGIST, "investigations"),
        (GUIDELINE_RAG, "options"),
        (VERIFIER, "verdicts"),
    ],
)
def test_each_agent_gets_well_shaped_demo_payload(demo_llm, system, key):
    result = LLMClient().complete_json(system, "Presenting complaint: fever and cough for 3 days")
    assert result["_demo"] is True
    assert key in result


def test_demo_payloads_are_marked(demo_llm):
    result = LLMClient().complete_json(VERIFIER, "fever")
    blob = str(result)
    assert demo_data.DEMO_TAG in blob


def test_scenario_selection_keywords():
    assert demo_data.select_scenario("severe chest pain on exertion").key == "cardiac"
    assert demo_data.select_scenario("fever with productive cough").key == "respiratory"
    assert demo_data.select_scenario("vague tiredness").key == "general"


# --------------------------------------------------------------------- unit: extraction


def test_extraction_returns_sample_data_in_demo_mode(demo_llm):
    pipeline = ExtractionPipeline()
    # Unintelligible bytes + no provider -> simulated sample extraction.
    result = pipeline.run(b"\x00\x01\x02not-text", "image/png")
    assert result.entities, "demo extraction should produce sample entities"
    assert result.model and demo_data.DEMO_TAG in result.model
    types = {e.entity_type for e in result.entities}
    assert "medication" in types


def test_extraction_empty_when_demo_disabled(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "llm_demo_fallback", False)
    result = ExtractionPipeline().run(b"\x00\x01\x02", "image/png")
    assert result.entities == []


# --------------------------------------------------------------------- integration: engine


async def _run_demo_case(client, complaint: str) -> dict:
    patient = await create_patient(client)
    resp = await client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": complaint},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["session"]["id"]

    # Answer all pending intake questions until complete.
    for _ in range(4):
        pending = (await client.get(f"/api/v1/reasoning/{session_id}/intake")).json()
        if not pending:
            break
        answers = [{"question_id": q["id"], "answer_text": "yes"} for q in pending]
        state = (
            await client.post(
                f"/api/v1/reasoning/{session_id}/intake/answers", json={"answers": answers}
            )
        ).json()
        if state["intake_complete"]:
            break

    resp = await client.post(f"/api/v1/reasoning/{session_id}/run")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_reasoning_runs_end_to_end_in_demo_mode(auth_client, demo_llm):
    result = await _run_demo_case(auth_client, "central chest pain radiating to the left arm")

    # The case is flagged as demo so the UI can show a banner.
    assert result["case_state"]["demo_mode"] is True

    suggestions = result["suggestions"]
    assert suggestions, "demo mode must still produce clinical suggestions"
    # Every suggestion carries a valid autonomy tier and routed through the verifier (Rule #1).
    tiers = ("informational", "suggestive", "flag_for_review")
    assert all(s["autonomy_tier"] in tiers for s in suggestions)
    assert result["case_state"]["verifier_status"] in (
        "agree",
        "partial_disagreement",
        "major_disagreement",
    )

    # Differential output is present and clearly marked as demo.
    diffs = [s for s in suggestions if s["output_type"] in ("differential", "cant_miss")]
    assert diffs
    body_blob = " ".join((s.get("body") or "") for s in suggestions)
    assert demo_data.DEMO_TAG in body_blob

    # Deterministic safety floor still applies on top of simulated data: chest pain forces
    # can't-miss diagnoses and therefore flag-for-review (conservative wins).
    cant_miss = [s for s in suggestions if s["cant_miss_flag"]]
    assert cant_miss
    assert result["session"]["autonomy_tier"] == "flag_for_review"


@pytest.mark.asyncio
async def test_demo_mode_produces_management_options(auth_client, demo_llm):
    result = await _run_demo_case(auth_client, "fever and cough for three days")
    mgmt = [s for s in result["suggestions"] if s["output_type"] == "management"]
    assert mgmt, "demo mode should surface illustrative management options"


# --------------------------------------------------------------------- integration: health


@pytest.mark.asyncio
async def test_health_reports_demo_mode(client, demo_llm):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_mode"] == "demo"
    assert body["llm_simulated"] is True

    resp = await client.get("/health/dependencies")
    body = resp.json()
    assert body["llm_mode"] == "demo"
    assert body["llm_demo_fallback"] is True
    assert body["llm_simulated"] is True


# ------------------------------------------------- production never serves simulated reasoning


def test_demo_fallback_is_disabled_in_production_even_when_the_flag_is_on(demo_llm, monkeypatch):
    """Defence in depth behind the startup config gate.

    ``assert_production_config`` refuses to start production with LLM_DEMO_FALLBACK=true, but it
    runs in the app lifespan. Any path that skips lifespan must still never hand a clinician
    fabricated ``[DEMO MODE]`` reasoning about a real patient.
    """
    monkeypatch.setattr(settings, "app_env", "production")
    assert demo_fallback_enabled() is False
    assert using_simulated_llm() is False
    assert is_available() is False


def test_complete_json_raises_in_production_instead_of_simulating(demo_llm, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    with pytest.raises(LLMUnavailable):
        LLMClient().complete_json(HYPOTHESIS_PANEL, "central chest pain")


def test_production_provider_failure_degrades_rather_than_simulating(monkeypatch):
    """With a key configured but every call failing, production degrades — it does not invent."""
    monkeypatch.setattr(settings, "openai_api_key", "sk-live")
    monkeypatch.setattr(settings, "llm_demo_fallback", True)
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "llm_retry_backoff_base_seconds", 0.0)
    monkeypatch.setattr(
        "app.agents.llm._complete_openai",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("upstream 503")),
    )
    with pytest.raises(LLMUnavailable):
        LLMClient().complete_json(HYPOTHESIS_PANEL, "central chest pain")


@pytest.mark.asyncio
async def test_health_reports_offline_not_demo_in_production(client, demo_llm, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    body = (await client.get("/health")).json()
    assert body["llm_mode"] == "offline"
    assert body["llm_simulated"] is False

    body = (await client.get("/health/dependencies")).json()
    assert body["llm_demo_fallback"] is False
    assert body["llm_simulated"] is False


# --------------------------------------------------------------------- unit: demo payload shaping


def test_hypothesis_payload_merges_all_specialties_when_none_is_detected():
    """A system prompt with no recognisable specialty still yields a usable differential."""
    scn = demo_data.select_scenario("chest pain")
    payload = demo_data._hypothesis_payload(scn, "no specialty here")
    names = {h["diagnosis_name"] for h in payload["hypotheses"]}
    # Union across Cardiology + General Internal Medicine + Primary Care for the cardiac scenario.
    assert "Stable angina (suspected)" in names
    assert "Gastro-oesophageal reflux disease" in names
    assert "Musculoskeletal chest wall pain" in names


def test_hypothesis_payload_scopes_to_the_detected_specialty():
    scn = demo_data.select_scenario("fever and productive cough")
    system = "You are on a multidisciplinary panel: Infectious Disease"
    payload = demo_data._hypothesis_payload(scn, system)
    names = {h["diagnosis_name"] for h in payload["hypotheses"]}
    assert names == {
        "Community-acquired pneumonia (suspected)",
        "Pulmonary tuberculosis (to consider)",
    }


def test_hypothesis_payload_is_empty_for_a_specialty_with_no_scenario_input():
    scn = demo_data.select_scenario("fever and productive cough")
    payload = demo_data._hypothesis_payload(scn, "multidisciplinary panel: Cardiology")
    assert payload["hypotheses"] == []


def test_devils_payload_takes_the_leading_hypothesis_from_the_case_text():
    scn = demo_data.select_scenario("chest pain")
    user = "Case summary\nLeading hypothesis: Aortic dissection\nOther"
    payload = demo_data._devils_payload(scn, user)
    assert payload["leading_hypothesis"] == "Aortic dissection"


def test_devils_payload_falls_back_to_the_scenario_leading_when_the_label_is_blank():
    scn = demo_data.select_scenario("chest pain")
    payload = demo_data._devils_payload(scn, "Leading hypothesis:   ")
    assert payload["leading_hypothesis"] == scn.leading


def test_devils_payload_falls_back_when_the_case_text_has_no_leading_label():
    scn = demo_data.select_scenario("chest pain")
    assert demo_data._devils_payload(scn, "")["leading_hypothesis"] == scn.leading


def test_simulated_response_returns_a_bare_marked_payload_for_an_unknown_agent():
    """An unrecognised system prompt must still be marked as demo, never raise."""
    payload = demo_data.simulated_response("You are some brand new agent", "fever")
    assert payload == {"_demo": True}


def test_select_scenario_prefers_the_scenario_with_more_keyword_hits():
    # "chest pain" (cardiac) vs "fever"+"cough"+"breathless" (respiratory) -> respiratory wins.
    scn = demo_data.select_scenario("fever with cough and breathless, mild chest pain")
    assert scn.key == "respiratory"


def test_select_scenario_handles_empty_input():
    assert demo_data.select_scenario("").key == "general"


def test_extraction_payload_is_marked_and_well_shaped():
    payload = demo_data.extraction_payload()
    assert payload["_demo"] is True
    assert payload["document_type"] == "prescription"
    types = {e["entity_type"] for e in payload["entities"]}
    assert types == {"medication", "lab_result", "condition"}
    # Every demo entity is visibly tagged or carries deliberately low confidence.
    for entity in payload["entities"]:
        assert entity["confidence"], "demo entities must carry confidence scores"
        assert max(entity["confidence"].values()) <= 0.6
