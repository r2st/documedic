"""Bounds on the clinical values a client can push into the API.

Three inputs reached real work without a bound on their size or their type, and each failed in
its own way rather than as a clean 422:

* ``drug_name`` on the safety check, which is quoted back verbatim in the "could not be
  matched" message and, on a miss, is scored by rapidfuzz against the whole vocabulary.
* ``value`` on an extraction correction, typed ``Any`` — so a dict reached the drug resolver's
  ``.strip()`` as a 500, and a 100 KB string was written toward a ``String(500)`` column.
* ``condition_name`` in the pathway URL, reflected into the not-found message unbounded.

These tests fix the boundary at the schema, where a rejection is a named field and a
constraint the client can act on, rather than at the database or in a stack trace.
"""

from __future__ import annotations

import pytest

from app.schemas.document import MAX_FIELD_VALUE_CHARS
from app.schemas.safety import MAX_DRUG_NAME_CHARS, MAX_DRUG_REFERENCE_ID_CHARS
from tests.conftest import create_patient

PRESCRIPTION = b"%PDF-1.4\nMEDICATIONS:\nGlycomet 500mg BD\n"


async def _document(client, patient_id) -> dict:
    resp = await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _approve(client, patient_id, doc_id, value, field="brand_name_raw"):
    return await client.post(
        f"/api/v1/patients/{patient_id}/documents/{doc_id}/approve",
        json={"corrections": [{"entity_index": 0, "field_name": field, "value": value}]},
    )


# --- drug names on the safety check -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_oversized_drug_name_is_rejected_before_it_is_resolved(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Paracetamol " * 5000},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"][-1] == "drug_name"


@pytest.mark.asyncio
async def test_the_rejection_does_not_echo_the_oversized_name_back(auth_client):
    """The 422 has to stay small, which is the whole point of catching it at the schema.

    An unresolved name that *is* within bounds is quoted back on purpose — the clinician needs
    to see what was looked up — so before the cap a 60 KB name produced a 60 KB response body
    and a 60 KB toast. `_safe_validation_errors` drops the rejected input; this asserts it.
    """
    patient = await create_patient(auth_client)
    marker = "Zzyzxine"
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": marker * 5000},
    )
    assert resp.status_code == 422
    assert marker not in resp.text
    assert len(resp.text) < 1000


@pytest.mark.asyncio
async def test_an_oversized_reference_id_is_rejected(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_reference_id": "D" * (MAX_DRUG_REFERENCE_ID_CHARS + 1)},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"][-1] == "drug_reference_id"


@pytest.mark.asyncio
async def test_a_realistic_brand_name_still_resolves(auth_client):
    """The bound has to leave real clinical input alone — including the Indian brand names
    that are the point of the vocabulary."""
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check", json={"drug_name": "Crocin"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["proposed_drug_name"].lower().startswith("paracetamol")


@pytest.mark.asyncio
async def test_a_name_at_the_limit_is_accepted_as_input(auth_client):
    """At the boundary the schema must not be what rejects it. It resolves to nothing, so the
    422 comes from the resolver instead — with the drug quoted, which is the point."""
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Q" * MAX_DRUG_NAME_CHARS},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"
    assert "could not be matched" in resp.json()["message"]


# --- extraction correction values ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_structured_correction_value_is_a_422_not_a_500(auth_client):
    """A dict used to reach ``DrugResolver.resolve``, which calls ``.strip()`` on it.

    That surfaced as an AttributeError, an unhandled 500, and a rolled-back approval — for
    what is simply a malformed field the client can be told about by name.
    """
    patient = await create_patient(auth_client)
    doc = await _document(auth_client, patient["id"])

    resp = await _approve(auth_client, patient["id"], doc["id"], {"nested": ["Glycomet"]})

    assert resp.status_code == 422, resp.text
    assert "value" in resp.json()["detail"][0]["loc"]


@pytest.mark.asyncio
async def test_a_list_correction_value_is_rejected_too(auth_client):
    patient = await create_patient(auth_client)
    doc = await _document(auth_client, patient["id"])
    resp = await _approve(auth_client, patient["id"], doc["id"], ["Glycomet", "Metformin"])
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_correction_value_too_long_for_its_column_is_rejected(auth_client):
    """``brand_name_raw`` is ``String(500)``. PostgreSQL rejects anything longer at flush —
    an unhandled DataError and a 500 — while SQLite keeps it, which is why the suite never
    caught this. The schema now rejects it on both."""
    patient = await create_patient(auth_client)
    doc = await _document(auth_client, patient["id"])

    resp = await _approve(auth_client, patient["id"], doc["id"], "X" * (MAX_FIELD_VALUE_CHARS + 1))

    assert resp.status_code == 422, resp.text
    assert "value" in resp.json()["detail"][0]["loc"]


@pytest.mark.asyncio
async def test_a_negative_entity_index_is_rejected(auth_client):
    """The service bounds-checks it, so a negative index was silently ignored rather than
    applied — a correction the clinician made and watched do nothing."""
    patient = await create_patient(auth_client)
    doc = await _document(auth_client, patient["id"])
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
        json={"corrections": [{"entity_index": -1, "field_name": "dose", "value": "500"}]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_the_scalar_values_a_real_correction_uses_all_still_work(auth_client):
    """Strings, numbers, booleans and null are what extraction actually produces."""
    patient = await create_patient(auth_client)

    for value in ("Metformin", 500, 12.5, True, None):
        doc = await _document(auth_client, patient["id"])
        resp = await _approve(auth_client, patient["id"], doc["id"], value)
        assert resp.status_code == 200, f"{value!r} rejected: {resp.text}"


@pytest.mark.asyncio
async def test_a_correction_at_the_length_limit_is_accepted_and_merged(auth_client):
    patient = await create_patient(auth_client)
    doc = await _document(auth_client, patient["id"])

    resp = await _approve(auth_client, patient["id"], doc["id"], "M" * MAX_FIELD_VALUE_CHARS)

    assert resp.status_code == 200, resp.text
    assert resp.json()["merged"]["medications"] == 1


@pytest.mark.asyncio
async def test_an_ordinary_correction_reaches_the_record(auth_client):
    """The guard rails must not have broken the thing they guard."""
    patient = await create_patient(auth_client)
    doc = await _document(auth_client, patient["id"])

    approve = await _approve(auth_client, patient["id"], doc["id"], "Glycomet 500")
    assert approve.status_code == 200, approve.text

    record = await auth_client.get(f"/api/v1/patients/{patient['id']}/record")
    brands = [m["brand_name_raw"] for m in record.json()["medications"]]
    assert "Glycomet 500" in brands


# --- pathway lookup -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_oversized_condition_name_is_rejected_without_being_reflected(auth_client):
    marker = "Zzyzx"
    resp = await auth_client.get("/api/v1/pathways/" + marker * 2000)
    assert resp.status_code == 422
    assert marker not in resp.text
    assert len(resp.text) < 1000


@pytest.mark.asyncio
async def test_a_real_condition_name_still_returns_its_pathway(auth_client):
    available = (await auth_client.get("/api/v1/pathways")).json()
    assert available, "no curated pathways seeded"

    resp = await auth_client.get(f"/api/v1/pathways/{available[0]}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["stages"]


@pytest.mark.asyncio
async def test_an_unknown_but_plausible_condition_still_gets_the_helpful_404(auth_client):
    """Bounding the segment must not have cost the message that lists what *is* available."""
    resp = await auth_client.get("/api/v1/pathways/acute_hippopotamus")
    assert resp.status_code == 404
    assert resp.json()["code"] == "pathway_not_found"
    assert "Available:" in resp.json()["message"]
