"""Integration: patient PII is genuinely encrypted at rest, not just decrypted transparently."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.conftest import create_patient


@pytest.mark.asyncio
async def test_stored_full_name_is_ciphertext_not_plaintext(auth_client, db):
    patient = await create_patient(auth_client, full_name="Ramesh Kumar", phone="9990001111")

    row = (
        await db.execute(
            text("SELECT full_name, phone, date_of_birth FROM patients WHERE id = :id"),
            {"id": patient["id"].replace("-", "")},
        )
    ).one()
    assert "Ramesh" not in row.full_name
    assert "Kumar" not in row.full_name
    assert "9990001111" not in row.phone
    assert "1968" not in row.date_of_birth


@pytest.mark.asyncio
async def test_api_still_returns_decrypted_plaintext(auth_client):
    patient = await create_patient(auth_client, full_name="Ramesh Kumar", phone="9990001111")
    got = await auth_client.get(f"/api/v1/patients/{patient['id']}")
    body = got.json()
    assert body["full_name"] == "Ramesh Kumar"
    assert body["phone"] == "9990001111"
    assert body["date_of_birth"] == "1968-05-10"


@pytest.mark.asyncio
async def test_two_patients_with_same_name_have_different_ciphertext(auth_client, db):
    """Nondeterministic encryption: identical plaintext must not produce identical ciphertext
    (otherwise ciphertext equality would leak whether two patients share a name)."""
    p1 = await create_patient(auth_client, full_name="Same Name", phone="1111111111")
    p2 = await create_patient(auth_client, full_name="Same Name", phone="2222222222")

    rows = (
        await db.execute(
            text("SELECT id, full_name FROM patients WHERE id IN (:a, :b)"),
            {"a": p1["id"].replace("-", ""), "b": p2["id"].replace("-", "")},
        )
    ).all()
    ciphertexts = {r.full_name for r in rows}
    assert len(ciphertexts) == 2
