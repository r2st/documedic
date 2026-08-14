"""The last four unexercised branches in the backend, each a real failure mode.

None of these are reachable through a happy path, which is why they had no coverage — and each
one is a fallback whose whole job is to behave correctly when something upstream has already
gone wrong. An untested fallback is the worst kind: it only ever runs on the day it matters.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.lab_result import lab_observation_key
from app.services.extraction.text_parser import ParsedEntity, ParsedField, _apply_sample_date
from tests.conftest import create_patient

# --------------------------------------------------------------------------------------
# lab_result._canonical_number: a dedup key for a value that is not a number
# --------------------------------------------------------------------------------------


def test_dedup_key_is_defined_for_a_non_numeric_value():
    """The key is a column default over whatever reached the INSERT, so it cannot raise.

    ``LabResult.value_numeric`` is nullable and the merge drops values it cannot parse, but the
    default runs against whatever was handed to the insert — and a key that raised would fail
    the row rather than deduplicate it.
    """
    key = lab_observation_key(None, "Potassium", "not a number", None)
    assert isinstance(key, str) and key


def test_non_numeric_values_still_distinguish_observations():
    """Falling back to the text must keep the key *distinguishing*, not collapse everything.

    Two different unreadable values are two different observations; a fallback that returned a
    constant would silently deduplicate them into one row.
    """
    a = lab_observation_key(None, "Potassium", "trace", None)
    b = lab_observation_key(None, "Potassium", "abundant", None)
    assert a != b
    assert a == lab_observation_key(None, "Potassium", "trace", None)


def test_numeric_spellings_of_one_value_share_a_key():
    """The reason the fallback exists at all: 3.0 and 3.000000 are the same observation."""
    assert lab_observation_key(None, "K", "3.0", None) == lab_observation_key(
        None, "K", "3.000000", None
    )


# --------------------------------------------------------------------------------------
# document_service: an integrity error that is *not* the duplicate-observation constraint
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrelated_integrity_error_is_not_reported_as_a_concurrent_approval(auth_client):
    """A genuine constraint failure must not be relabelled as "someone else got there first".

    ``ConcurrentApprovalError`` tells the clinician the work is already done and to reload —
    advice that is actively wrong for any other integrity failure, and that would hide a real
    defect behind a plausible-sounding 409.
    """
    patient = await create_patient(auth_client, full_name="Integrity Probe")
    upload = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={
            "file": (
                "labs.pdf",
                b"%PDF-1.4\nLABS:\nCreatinine: 1.1 mg/dL (0.6-1.2)\n",
                "application/pdf",
            )
        },
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["id"]
    await auth_client.get(f"/api/v1/patients/{patient['id']}/documents/{document_id}/extraction")

    unrelated = IntegrityError(
        "INSERT ...", {}, Exception("null value in column violates not-null")
    )
    with patch("app.services.document_service.GraphService.merge_entities", side_effect=unrelated):
        with pytest.raises(IntegrityError):
            await auth_client.post(
                f"/api/v1/patients/{patient['id']}/documents/{document_id}/approve",
                json={"corrections": [], "rejected_entity_indexes": []},
            )


# --------------------------------------------------------------------------------------
# text_parser._apply_sample_date: a row's own date outranks the report's
# --------------------------------------------------------------------------------------


def _lab(*fields: ParsedField) -> ParsedEntity:
    return ParsedEntity(entity_type="lab_result", fields=list(fields))


def test_report_date_does_not_overwrite_a_rows_own_sample_date():
    """A collection date printed against one row is better evidence than the report header.

    Overwriting it would move an observation to the wrong day, which is exactly the error a
    longitudinal record cannot absorb: trends and "latest per marker" both read this field.
    """
    own = ParsedField("sample_date", "2026-03-01T00:00:00", 0.9)
    entity = _lab(ParsedField("marker_name", "Creatinine", 0.9), own)

    _apply_sample_date([entity], datetime(2026, 4, 15, tzinfo=UTC), 0.5)

    dates = [f for f in entity.fields if f.name == "sample_date"]
    assert len(dates) == 1, "the report date was appended alongside the row's own"
    assert dates[0].value == "2026-03-01T00:00:00"
    assert dates[0].confidence == 0.9


def test_report_date_fills_in_a_row_that_has_none():
    entity = _lab(ParsedField("marker_name", "Sodium", 0.9))

    _apply_sample_date([entity], datetime(2026, 4, 15, tzinfo=UTC), 0.5)

    stamped = next(f for f in entity.fields if f.name == "sample_date")
    assert stamped.value.startswith("2026-04-15")
    # Lower confidence than a collection date, so the clinician is asked to confirm it.
    assert stamped.confidence == 0.5


def test_report_date_is_not_stamped_onto_non_lab_entities():
    med = ParsedEntity(
        entity_type="medication", fields=[ParsedField("brand_name_raw", "Dolo", 0.9)]
    )

    _apply_sample_date([med], datetime(2026, 4, 15, tzinfo=UTC), 0.5)

    assert all(f.name != "sample_date" for f in med.fields)


# --------------------------------------------------------------------------------------
# guideline_service: the corpus cache is bounded
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corpus_cache_is_bounded(db):
    """Unbounded, this grows once per corpus version for the life of the process.

    Each entry holds a whole tokenised guideline corpus, so the ceiling is what keeps a
    long-running API from accumulating every version it has ever been asked for.
    """
    from app.services import guideline_service as gs

    service = gs.GuidelineService(db)
    for filler in range(gs._MAX_CACHED_CORPUS_VERSIONS):
        gs._CORPUS_CACHE[f"filler-{filler}"] = ((0, None), ())
    assert len(gs._CORPUS_CACHE) == gs._MAX_CACHED_CORPUS_VERSIONS

    await service._load_corpus(f"fresh-{uuid.uuid4()}")

    # Cleared wholesale rather than evicted one at a time; either way it must not have grown.
    assert len(gs._CORPUS_CACHE) <= gs._MAX_CACHED_CORPUS_VERSIONS
