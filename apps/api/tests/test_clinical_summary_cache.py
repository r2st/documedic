"""Re-serving a summary written from a chart that has not changed.

The summary is the only per-patient read in this API that costs a provider call, and it is the
shape a ward-round tool or a handover screen fires on every page load. The saving is real; the
hazard is that caching *clinical prose* is the one place where serving a stale answer is not slow
but wrong — a handover paragraph that omits an allergy charted five minutes ago is a clinical
error dressed as a fast response.

So the tests here are almost entirely about the key. It is a hash of ``_prompt_text(facts)``, the
exact string the model is asked about, which means a hit is a guarantee that the model would be
asked a byte-identical question. Every test below that charts something and expects a miss is
asserting the same property from a different direction: there is no table to remember to
invalidate, because the cache key *is* the input.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from app.config import settings
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.summary_service import ClinicalSummaryService

pytestmark = pytest.mark.asyncio


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"cache-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Cache Patient",
        sex="female",
        date_of_birth=date(1972, 3, 4),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _chart_something(db, patient: Patient) -> None:
    db.add(
        Condition(
            patient_id=patient.id,
            condition_name="Type 2 diabetes mellitus",
            status="active",
            onset_date=date(2019, 6, 1),
        )
    )
    await db.flush()


class _CountingProvider:
    """A stub that answers, and counts how many times it was asked."""

    def __init__(self, overview: str = "Longstanding type 2 diabetes.") -> None:
        self.calls = 0
        self.overview = overview

    async def __call__(self, *_args, **_kwargs):
        self.calls += 1
        return {
            "overview": f"{self.overview} (call {self.calls})",
            "active_problems": ["Type 2 diabetes mellitus"],
            "current_medications": [],
            "recent_investigations": [],
            "recent_encounters": [],
            "record_gaps": [],
        }


def _live(monkeypatch, provider) -> None:
    monkeypatch.setattr("app.services.summary_service.available_providers", lambda: ["openai"])
    monkeypatch.setattr("app.services.summary_service.complete_json_off_loop", provider)


async def test_an_unchanged_chart_is_not_summarised_twice(db, monkeypatch) -> None:
    provider = _CountingProvider()
    _live(monkeypatch, provider)
    _, patient = await _account_and_patient(db)
    await _chart_something(db, patient)
    service = ClinicalSummaryService(db)

    first = await service.summarize(patient)
    second = await service.summarize(patient)

    assert provider.calls == 1
    assert second["summary"]["overview"] == first["summary"]["overview"]
    assert first["cached"] is False
    assert second["cached"] is True


async def test_a_cached_summary_says_when_it_was_written_not_when_it_was_served(
    db, monkeypatch
) -> None:
    """The honesty requirement. Stamping the response time would let a re-read of an unchanged
    chart claim to be a fresh reading of it, and "how old is this summary" is a question a
    clinician on a ward round is entitled to a true answer to."""
    _live(monkeypatch, _CountingProvider())
    _, patient = await _account_and_patient(db)
    await _chart_something(db, patient)
    service = ClinicalSummaryService(db)

    first = await service.summarize(patient)
    second = await service.summarize(patient)

    assert second["generated_at"] == first["generated_at"]


@pytest.mark.parametrize(
    ("what", "row"),
    [
        (
            "an allergy",
            lambda pid: Allergy(
                patient_id=pid, allergen_name="Penicillin", allergen_type="drug", status="active"
            ),
        ),
        (
            "a medication",
            lambda pid: MedicationEvent(
                patient_id=pid,
                event_type="start",
                is_current=True,
                generic_name="Metformin",
                dose="500",
                dose_unit="mg",
                event_date=date(2025, 1, 1),
            ),
        ),
        (
            "a lab result",
            lambda pid: LabResult(
                patient_id=pid,
                marker_name="HbA1c",
                value_numeric=9.1,
                unit="%",
                sample_date=datetime(2025, 11, 3, tzinfo=UTC),
            ),
        ),
        (
            "another condition",
            lambda pid: Condition(patient_id=pid, condition_name="Hypertension", status="active"),
        ),
    ],
)
async def test_anything_charted_since_misses_the_cache(db, monkeypatch, what, row) -> None:
    """One case per section the summary reads. Each is a table a fingerprint built by *listing*
    tables could have been forgotten from — and the failure of forgetting one would be a handover
    that silently omits it. Keying on the prompt text makes the list impossible to get wrong."""
    provider = _CountingProvider()
    _live(monkeypatch, provider)
    _, patient = await _account_and_patient(db)
    await _chart_something(db, patient)
    service = ClinicalSummaryService(db)
    await service.summarize(patient)

    db.add(row(patient.id))
    await db.flush()
    second = await service.summarize(patient)

    assert provider.calls == 2, f"{what} charted since should not have hit the cache"
    assert second["cached"] is False


async def test_two_patients_with_identical_charts_do_not_share_an_entry(db, monkeypatch) -> None:
    """Their prose would be equally valid, which is exactly why this is worth pinning: a cache
    that quietly shares entries between records is not a property anyone should have to
    discover from behaviour."""
    provider = _CountingProvider()
    _live(monkeypatch, provider)
    _, first_patient = await _account_and_patient(db)
    _, second_patient = await _account_and_patient(db)
    await _chart_something(db, first_patient)
    await _chart_something(db, second_patient)
    service = ClinicalSummaryService(db)

    await service.summarize(first_patient)
    await service.summarize(second_patient)

    assert provider.calls == 2


async def test_a_degraded_answer_is_never_stored(db, monkeypatch) -> None:
    """Caching one would make an outage outlive itself — serving "the provider was down" for
    minutes after it came back, on a chart nobody touched in between."""
    monkeypatch.setattr("app.services.summary_service.available_providers", lambda: [])
    _, patient = await _account_and_patient(db)
    await _chart_something(db, patient)
    service = ClinicalSummaryService(db)
    offline = await service.summarize(patient)
    assert offline["source"] == "deterministic"

    provider = _CountingProvider()
    _live(monkeypatch, provider)
    recovered = await service.summarize(patient)

    assert provider.calls == 1
    assert recovered["source"] == "model"
    assert recovered["cached"] is False


async def test_a_cache_hit_still_reports_that_framing_had_to_fire(db, monkeypatch) -> None:
    """The subtle one, and the reason the stored narrative is copied on both sides.

    ``_frame`` rewrites in place. Handing a cache hit the stored object would let the first
    request's framing pass mutate it, so every later hit would report
    ``prescriber_framing_applied: false`` — extinguishing the only signal that says a provider
    ignored Rule #4, for exactly the charts that are read most often.
    """

    async def _imperative(*_args, **_kwargs):
        return {
            "overview": "Give metformin 500 mg twice daily.",
            "active_problems": ["Start amlodipine for the hypertension."],
            "current_medications": [],
            "recent_investigations": [],
            "recent_encounters": [],
            "record_gaps": [],
        }

    _live(monkeypatch, _imperative)
    _, patient = await _account_and_patient(db)
    await _chart_something(db, patient)
    service = ClinicalSummaryService(db)

    first = await service.summarize(patient)
    second = await service.summarize(patient)

    assert first["prescriber_framing_applied"] is True
    assert second["prescriber_framing_applied"] is True
    assert second["summary"]["overview"] == first["summary"]["overview"]
    assert "Give metformin" not in second["summary"]["overview"]


async def test_the_cache_can_be_switched_off(db, monkeypatch) -> None:
    """Zero on either bound. A deployment that would rather pay for every call, or that does not
    want patient-derived prose held in process memory at all, has one setting to say so."""
    from app.core.ttl_cache import TTLCache

    monkeypatch.setattr(
        "app.services.summary_service._narrative_cache",
        TTLCache(max_entries=0, ttl_seconds=settings.summary_cache_ttl_seconds),
    )
    provider = _CountingProvider()
    _live(monkeypatch, provider)
    _, patient = await _account_and_patient(db)
    await _chart_something(db, patient)
    service = ClinicalSummaryService(db)

    await service.summarize(patient)
    await service.summarize(patient)

    assert provider.calls == 2


async def test_the_route_reports_the_hit_and_the_trail_records_it(auth_client, monkeypatch, db):
    """``source: "model"`` is what tells a DPDP reviewer the chart was sent to a third-party
    provider. On a cache hit it was not, and a trail that claimed otherwise would be recording a
    processing event that never happened."""
    from tests.conftest import create_patient

    _live(monkeypatch, _CountingProvider())
    created = await create_patient(auth_client)
    patient = await db.get(Patient, uuid.UUID(created["id"]))
    assert patient is not None
    await _chart_something(db, patient)
    await db.commit()

    first = await auth_client.get(f"/api/v1/patients/{created['id']}/summary")
    second = await auth_client.get(f"/api/v1/patients/{created['id']}/summary")

    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    trail = (await auth_client.get(f"/api/v1/patients/{created['id']}/audit")).json()
    entries = [e for e in trail["items"] if e["action"] == "clinical_summary_generated"]
    assert sorted(e["payload"]["cached"] for e in entries) == [False, True]
