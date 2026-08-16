"""A handover summary of a chart that is already in the record.

Why this is not a Verifier bypass
---------------------------------
Critical Safety Rule #1 gates every clinical *output* — a differential, an investigation, a
management option — behind the Verifier. This route produces none of those: it restates what is
already charted, writes no ``ClinicalSuggestion``, and assigns no autonomy tier. The claim is
only worth anything if it is enforced rather than asserted, so the tests below pin the three
things that enforce it:

* the deterministic Rule #4 control runs over every string the model returned, so an imperative
  sentence reaches the clinician re-framed and the response says the rewrite fired;
* the charted facts come back beside the prose, so the evidence is on the same screen as the
  conclusion (Rule #6);
* the simulated demo provider is never used, because a fabricated handover reads as a statement
  about *this* patient — the distinction R57 was about, one layer further out.

And the honest-failure half: with no provider reachable the endpoint answers with the chart
restated and says ``degraded``, rather than 503ing or quietly serving something that looks like
a summary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.encounter import Encounter
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.summary_service import ClinicalSummaryService

from .conftest import create_patient

pytestmark = pytest.mark.asyncio


async def _account_and_patient(db, **patient_fields) -> tuple[Account, Patient]:
    account = Account(email=f"summary-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    fields = {
        "full_name": "Summary Patient",
        "sex": "female",
        "date_of_birth": date(1972, 3, 4),
        "consent_given": True,
        "consent_given_at": datetime.now(UTC),
    }
    fields.update(patient_fields)
    patient = Patient(account_id=account.id, **fields)
    db.add(patient)
    await db.flush()
    return account, patient


async def _populate(db, patient: Patient) -> None:
    db.add(
        Condition(
            patient_id=patient.id,
            condition_name="Type 2 diabetes mellitus",
            status="active",
            onset_date=date(2019, 6, 1),
        )
    )
    db.add(
        Condition(
            patient_id=patient.id,
            condition_name="Acute bronchitis",
            status="resolved",
            onset_date=date(2021, 1, 1),
        )
    )
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            event_type="start",
            is_current=True,
            generic_name="Metformin",
            dose="500",
            dose_unit="mg",
            frequency="BD",
            event_date=date(2024, 2, 2),
        )
    )
    db.add(
        LabResult(
            patient_id=patient.id,
            marker_name="HbA1c",
            value_numeric=9.1,
            unit="%",
            is_abnormal=True,
            abnormality_direction="high",
            sample_date=datetime(2025, 11, 3, tzinfo=UTC),
        )
    )
    db.add(
        Encounter(
            patient_id=patient.id,
            encounter_date=date(2025, 11, 4),
            encounter_type="outpatient",
            presenting_complaint="Routine diabetes review",
            # Draft, not signed: ck_encounters_signature_complete requires the signature columns
            # alongside a signed status, and this fixture is about the summary rather than about
            # the encounter lifecycle.
            status="draft",
        )
    )
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name="Penicillin",
            allergen_type="drug",
            severity="severe",
            status="active",
        )
    )
    await db.flush()


# --- the chart half -------------------------------------------------------------------------


async def test_the_chart_carries_the_four_sections_the_summary_is_about(db) -> None:
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    facts = await ClinicalSummaryService(db).chart_facts(patient)

    assert [c["condition_name"] for c in facts.active_conditions] == ["Type 2 diabetes mellitus"]
    assert [m["generic_name"] for m in facts.current_medications] == ["Metformin"]
    assert [lab["marker_name"] for lab in facts.recent_labs] == ["HbA1c"]
    assert [e["encounter_type"] for e in facts.recent_encounters] == ["outpatient"]


async def test_a_resolved_condition_is_not_an_active_problem(db) -> None:
    """ "Active" means the same thing here as it does to the safety engine."""
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    facts = await ClinicalSummaryService(db).chart_facts(patient)

    assert "Acute bronchitis" not in [c["condition_name"] for c in facts.active_conditions]


async def test_allergies_are_carried_even_though_no_summary_field_asks_for_them(db) -> None:
    """The one part of a chart whose omission from a handover is dangerous, not just untidy."""
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    facts = await ClinicalSummaryService(db).chart_facts(patient)

    assert [a["allergen_name"] for a in facts.allergies] == ["Penicillin"]


async def test_an_unconfirmed_allergy_is_carried_rather_than_read_as_absent(db) -> None:
    _, patient = await _account_and_patient(db)
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name="Sulfa",
            allergen_type="drug",
            status="unknown",
        )
    )
    await db.flush()

    facts = await ClinicalSummaryService(db).chart_facts(patient)

    assert [a["allergen_name"] for a in facts.allergies] == ["Sulfa"]


async def test_the_age_is_derived_and_an_implausible_date_of_birth_yields_none(db) -> None:
    _, patient = await _account_and_patient(db, date_of_birth=date(2126, 1, 1))

    facts = await ClinicalSummaryService(db).chart_facts(patient)

    assert facts.age_years is None


# --- how it degrades ------------------------------------------------------------------------


async def test_an_empty_chart_is_answered_without_spending_a_provider_call(db) -> None:
    """A model asked to summarise nothing writes something."""
    _, patient = await _account_and_patient(db)

    result = await ClinicalSummaryService(db).summarize(patient)

    assert result["source"] == "empty"
    assert result["degraded"] is True
    assert result["summary"]["record_gaps"]


async def test_with_no_provider_the_chart_is_restated_rather_than_refused(db, monkeypatch) -> None:
    monkeypatch.setattr("app.services.summary_service.available_providers", lambda: [])
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    result = await ClinicalSummaryService(db).summarize(patient)

    assert result["source"] == "deterministic"
    assert result["degraded"] is True
    # Every line is a rendering of a row, so the medication is there under its own name.
    assert any("Metformin" in line for line in result["summary"]["current_medications"])


async def test_the_demo_provider_never_writes_a_handover_about_a_real_patient(
    db, monkeypatch
) -> None:
    """``is_available()`` is true with the demo net on and no key. That is the wrong gate here.

    A Reasoning Theatre nobody is treating from can show simulated deliberation; a handover
    paragraph is read as a statement about this patient, and a fabricated one is worse than no
    paragraph. This is R57's distinction — "is a key configured" versus "did the call work" —
    one layer further out.
    """
    calls: list[str] = []

    async def _never(*args, **kwargs):
        calls.append("called")
        return {"overview": "should not be reached"}

    monkeypatch.setattr("app.services.summary_service.available_providers", lambda: [])
    monkeypatch.setattr("app.services.summary_service.complete_json_off_loop", _never)
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    result = await ClinicalSummaryService(db).summarize(patient)

    assert calls == []
    assert result["source"] == "deterministic"


async def test_a_demo_marked_response_is_refused_even_when_a_key_is_configured(
    db, monkeypatch
) -> None:
    """A mid-call failover into the demo net is the same fabrication by another route."""

    async def _demo(*args, **kwargs):
        return {"_demo": True, "overview": "[DEMO MODE] A plausible-sounding patient."}

    monkeypatch.setattr("app.services.summary_service.available_providers", lambda: ["openai"])
    monkeypatch.setattr("app.services.summary_service.complete_json_off_loop", _demo)
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    result = await ClinicalSummaryService(db).summarize(patient)

    assert result["source"] == "deterministic"
    assert "[DEMO MODE]" not in result["summary"]["overview"]


async def test_a_well_formed_but_empty_answer_falls_back_rather_than_showing_nothing(
    db, monkeypatch
) -> None:
    """An empty handover for a chart with content reads as "there is nothing here"."""

    async def _blank(*args, **kwargs):
        return {"overview": "", "active_problems": []}

    monkeypatch.setattr("app.services.summary_service.available_providers", lambda: ["openai"])
    monkeypatch.setattr("app.services.summary_service.complete_json_off_loop", _blank)
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    result = await ClinicalSummaryService(db).summarize(patient)

    assert result["source"] == "deterministic"
    assert result["summary"]["current_medications"]


async def test_a_provider_failure_returns_none_and_degrades(db, monkeypatch) -> None:
    async def _failed(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.summary_service.available_providers", lambda: ["openai"])
    monkeypatch.setattr("app.services.summary_service.complete_json_off_loop", _failed)
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    result = await ClinicalSummaryService(db).summarize(patient)

    assert result["source"] == "deterministic"
    assert result["degraded"] is True


# --- Rule #4: the deterministic framing control ----------------------------------------------


async def test_an_imperative_sentence_from_the_model_is_reframed_before_it_is_served(
    db, monkeypatch
) -> None:
    """The prompt asks for prescriber framing. A prompt is a request, not a control."""

    async def _imperative(*args, **kwargs):
        return {
            "overview": "The patient has type 2 diabetes.",
            "active_problems": ["Give metformin 500 mg twice daily."],
        }

    monkeypatch.setattr("app.services.summary_service.available_providers", lambda: ["openai"])
    monkeypatch.setattr("app.services.summary_service.complete_json_off_loop", _imperative)
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    result = await ClinicalSummaryService(db).summarize(patient)

    assert result["prescriber_framing_applied"] is True
    assert "The patient has" not in result["summary"]["overview"]
    assert not result["summary"]["active_problems"][0].startswith("Give ")
    # The clinical substance survives the rewrite; only the modality moves.
    assert "metformin 500 mg" in result["summary"]["active_problems"][0]


async def test_already_framed_text_is_left_alone_and_the_flag_stays_down(db, monkeypatch) -> None:
    async def _framed(*args, **kwargs):
        return {
            "overview": "Findings are consistent with long-standing type 2 diabetes.",
            "active_problems": ["Type 2 diabetes mellitus, charted active since 2019."],
        }

    monkeypatch.setattr("app.services.summary_service.available_providers", lambda: ["openai"])
    monkeypatch.setattr("app.services.summary_service.complete_json_off_loop", _framed)
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    result = await ClinicalSummaryService(db).summarize(patient)

    assert result["prescriber_framing_applied"] is False
    assert result["source"] == "model"


async def test_a_list_field_answered_with_junk_shapes_does_not_reach_the_screen(
    db, monkeypatch
) -> None:
    """Every item here is rendered directly into a clinician's screen."""

    async def _junk(*args, **kwargs):
        return {
            "overview": "A summary.",
            "active_problems": "Type 2 diabetes",
            "current_medications": [{"drug": "Metformin"}, None, "", "Amlodipine 5 mg"],
        }

    monkeypatch.setattr("app.services.summary_service.available_providers", lambda: ["openai"])
    monkeypatch.setattr("app.services.summary_service.complete_json_off_loop", _junk)
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    result = await ClinicalSummaryService(db).summarize(patient)

    assert result["summary"]["active_problems"] == ["Type 2 diabetes"]
    assert all(isinstance(item, str) and item for item in result["summary"]["current_medications"])
    assert "Amlodipine 5 mg" in result["summary"]["current_medications"]


# --- what the prompt is given -----------------------------------------------------------------


async def test_the_record_reaches_the_model_fenced_as_untrusted_data(db, monkeypatch) -> None:
    """It is OCR of a document someone handed over. The boundary clause is not optional."""
    seen: dict[str, str] = {}

    async def _capture(client, system, user):
        seen["system"] = system
        seen["user"] = user
        return {"overview": "ok"}

    monkeypatch.setattr("app.services.summary_service.available_providers", lambda: ["openai"])
    monkeypatch.setattr("app.services.summary_service.complete_json_off_loop", _capture)
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    await ClinicalSummaryService(db).summarize(patient)

    assert "PATIENT RECORD" in seen["user"]
    assert "Metformin" in seen["user"]
    # The prompt refuses the tasks that would make this a clinical opinion.
    assert "NOT diagnosing" in seen["system"]


# --- over the wire ----------------------------------------------------------------------------


async def test_the_endpoint_puts_the_chart_before_the_summary(auth_client) -> None:
    """Rule #6, applied to a paragraph: evidence before conclusion, in declaration order."""
    patient = await create_patient(auth_client)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/summary")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    keys = list(body)
    assert keys.index("chart") < keys.index("summary")
    assert set(body["chart"]) >= {"active_conditions", "current_medications", "recent_labs"}


async def test_the_endpoint_reports_how_the_summary_was_produced(auth_client) -> None:
    patient = await create_patient(auth_client)

    body = (await auth_client.get(f"/api/v1/patients/{patient['id']}/summary")).json()

    assert body["source"] in {"model", "deterministic", "empty"}
    assert isinstance(body["degraded"], bool)
    assert isinstance(body["prescriber_framing_applied"], bool)


async def test_the_summary_read_is_on_the_audit_trail(auth_client) -> None:
    """A disclosure — and the only one that also sends the chart to a third party."""
    patient = await create_patient(auth_client)
    await auth_client.get(f"/api/v1/patients/{patient['id']}/summary")

    trail = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")).json()

    entries = [e for e in trail["items"] if e["action"] == "clinical_summary_generated"]
    assert len(entries) == 1
    assert entries[0]["payload"]["source"] in {"model", "deterministic", "empty"}


async def test_the_audit_payload_carries_counts_and_never_clinical_content(auth_client) -> None:
    """audit_logs.payload is unencrypted, immutable and never pruned."""
    patient = await create_patient(auth_client)
    await auth_client.get(f"/api/v1/patients/{patient['id']}/summary")

    trail = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")).json()
    payload = next(
        e["payload"] for e in trail["items"] if e["action"] == "clinical_summary_generated"
    )

    assert set(payload) == {
        "source",
        "degraded",
        "conditions",
        "medications",
        "labs",
        "encounters",
        "prescriber_framing_applied",
        # Beside `source`, and needed by it: `source: "model"` is what tells a DPDP reviewer the
        # chart was sent to a third-party provider, and a cache hit did not send it anywhere.
        "cached",
    }
    assert all(
        not isinstance(value, str) or value in {"model", "deterministic", "empty"}
        for value in payload.values()
    )


async def test_another_accounts_chart_cannot_be_summarised(auth_client, second_auth_client) -> None:
    patient = await create_patient(auth_client)

    resp = await second_auth_client.get(f"/api/v1/patients/{patient['id']}/summary")

    assert resp.status_code == 404


async def test_the_summary_route_carries_a_ceiling(monkeypatch, auth_client) -> None:
    """The one per-patient read in this API that reaches a provider."""
    monkeypatch.setattr("app.config.settings.rate_limit_summaries_per_minute", 1)
    patient = await create_patient(auth_client)

    first = await auth_client.get(f"/api/v1/patients/{patient['id']}/summary")
    second = await auth_client.get(f"/api/v1/patients/{patient['id']}/summary")

    assert first.status_code == 200
    assert second.status_code == 429
