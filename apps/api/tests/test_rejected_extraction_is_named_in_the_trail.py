"""Rejecting an extracted entity is a clinical decision, and the trail recorded only its count.

At document review a clinician ticks items off an extraction. Whatever they reject never reaches
the chart, and nothing downstream can then see it. For an allergy that is the sharp case:
Critical Safety Rule #3's hard block is evaluated against *charted* allergies, so dropping one at
review silently disables the block for that allergen, permanently, with no flag anywhere saying
a document had recorded it.

``extraction_approved`` recorded ``rejected_count`` and nothing else, and the count cannot be
turned back into the decision. The stored extraction keeps every entity whether it was merged or
not, and the merge deduplicates against what is already charted — so an entity that is absent
from the chart may have been rejected *or* may have already been there. "One of the four was
dropped" is not answerable into "the penicillin allergy was dropped" by any query over the data
that survives.

What is recorded now is position plus type: an index into the extraction that this entry's
``entity_id`` already points at, and a value from the closed ``MERGEABLE_ENTITY_TYPES``
vocabulary. Deliberately not the field values — ``audit_logs.payload`` is unencrypted, immutable
and never pruned, and the allergen name is on the document row the index resolves against. That
is the same shape ``critical_lab_value_not_evaluated`` uses, and the free-text sweep in
``test_audit_payload_free_text`` holds it to it.

The document itself is marked too, so the extraction carries the decision rather than only the
trail describing it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.document import Document
from tests.conftest import create_patient
from tests.test_documents import PRESCRIPTION

pytestmark = pytest.mark.asyncio


async def _upload_and_read(client, patient_id: str) -> tuple[str, list[dict]]:
    """Upload the sample prescription and return its id plus the extracted entities in order."""
    upload = await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()["id"]

    extraction = await client.get(f"/api/v1/patients/{patient_id}/documents/{doc_id}/extraction")
    assert extraction.status_code == 200, extraction.text
    return doc_id, extraction.json()["entities"]


def _index_of(entities: list[dict], entity_type: str) -> int:
    for idx, entity in enumerate(entities):
        if entity["entity_type"] == entity_type:
            return idx
    raise AssertionError(f"the sample prescription produced no {entity_type!r} entity")


async def _approval_entry(db) -> AuditLog:
    rows = await db.execute(
        select(AuditLog)
        .where(AuditLog.action == "extraction_approved")
        .order_by(AuditLog.sequence.desc())
        .limit(1)
    )
    entry = rows.scalar_one_or_none()
    assert entry is not None, "the approval wrote no extraction_approved entry"
    return entry


async def test_the_trail_names_which_entity_was_rejected(auth_client, db):
    """The whole point: "one was dropped" becomes "the allergy was dropped"."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    doc_id, entities = await _upload_and_read(auth_client, pid)
    allergy_index = _index_of(entities, "allergy")

    approve = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={"corrections": [], "rejected_entity_indexes": [allergy_index]},
    )
    assert approve.status_code == 200, approve.text

    entry = await _approval_entry(db)
    assert entry.payload["rejected_count"] == 1
    assert entry.payload["rejected"] == [{"index": allergy_index, "entity_type": "allergy"}]


async def test_a_rejected_allergy_really_is_absent_from_the_chart(auth_client, db):
    """The reason the entry matters: the hard block has nothing left to evaluate against."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    doc_id, entities = await _upload_and_read(auth_client, pid)

    approve = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={
            "corrections": [],
            "rejected_entity_indexes": [_index_of(entities, "allergy")],
        },
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["merged"]["allergies"] == 0

    record = await auth_client.get(f"/api/v1/patients/{pid}/record")
    assert record.json()["allergies"] == []


async def test_approving_everything_records_an_empty_rejection_list(auth_client, db):
    """The key is present on every approval, so its absence is never ambiguous."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    doc_id, _ = await _upload_and_read(auth_client, pid)

    approve = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approve.status_code == 200, approve.text

    entry = await _approval_entry(db)
    assert entry.payload["rejected"] == []
    assert entry.payload["rejected_count"] == 0


async def test_several_rejections_are_all_named_in_order(auth_client, db):
    patient = await create_patient(auth_client)
    pid = patient["id"]
    doc_id, entities = await _upload_and_read(auth_client, pid)
    indexes = sorted({_index_of(entities, "allergy"), _index_of(entities, "condition")})

    approve = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={"corrections": [], "rejected_entity_indexes": indexes},
    )
    assert approve.status_code == 200, approve.text

    entry = await _approval_entry(db)
    assert [item["index"] for item in entry.payload["rejected"]] == indexes
    assert {item["entity_type"] for item in entry.payload["rejected"]} == {"allergy", "condition"}


async def test_the_stored_extraction_carries_the_decision_too(auth_client, db):
    """So the document says what happened to each item, not only the trail describing it."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    doc_id, entities = await _upload_and_read(auth_client, pid)
    allergy_index = _index_of(entities, "allergy")

    approve = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={"corrections": [], "rejected_entity_indexes": [allergy_index]},
    )
    assert approve.status_code == 200, approve.text

    document = await db.get(Document, __import__("uuid").UUID(doc_id))
    assert document is not None
    await db.refresh(document)
    stored = document.extraction_metadata["entities"]
    assert stored[allergy_index]["rejected"] is True
    assert all(
        entity["rejected"] is False for idx, entity in enumerate(stored) if idx != allergy_index
    )


async def test_a_correction_on_a_re_approval_reaches_the_stored_extraction(auth_client, db):
    """The same in-place-mutation trap, on the field a clinician actually retyped.

    ``extraction_metadata`` is a plain JSON column, so SQLAlchemy emits an UPDATE only when the
    attribute's new value differs from the one it loaded — and applying a correction by writing
    into the nested dict changes both, because they are the same object. On the *first* approval
    the write rode along on ``meta["approved"] = True``; on a second one there was no other
    change to carry it, so the merge used the corrected dose and the stored extraction still
    showed the extractor's. The chart and the document then disagreed about what the clinician
    typed, and the document is the thing a reviewer opens.
    """
    patient = await create_patient(auth_client)
    pid = patient["id"]
    doc_id, entities = await _upload_and_read(auth_client, pid)
    med_index = _index_of(entities, "medication")

    first = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert first.status_code == 200, first.text

    second = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={
            "corrections": [{"entity_index": med_index, "field_name": "dose", "value": "850"}],
            "rejected_entity_indexes": [],
        },
    )
    assert second.status_code == 200, second.text

    reread = await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc_id}/extraction")
    corrected = reread.json()["entities"][med_index]
    dose = next(f for f in corrected["fields"] if f["name"] == "dose")
    assert dose["value"] == "850"
    assert dose["confidence"] == 1.0


async def test_a_later_approval_that_accepts_it_clears_the_mark(auth_client, db):
    """Re-approval is allowed (the merge deduplicates), so a stale mark would misreport it."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    doc_id, entities = await _upload_and_read(auth_client, pid)
    allergy_index = _index_of(entities, "allergy")

    first = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={"corrections": [], "rejected_entity_indexes": [allergy_index]},
    )
    assert first.status_code == 200, first.text

    second = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert second.status_code == 200, second.text
    assert second.json()["merged"]["allergies"] == 1

    document = await db.get(Document, __import__("uuid").UUID(doc_id))
    assert document is not None
    await db.refresh(document)
    assert all(entity["rejected"] is False for entity in document.extraction_metadata["entities"])

    entry = await _approval_entry(db)
    assert entry.payload["rejected"] == []
