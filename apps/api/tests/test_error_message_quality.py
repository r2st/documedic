"""Error messages a clinician actually reads.

These are not cosmetic assertions. An error shown mid-consultation has to answer "what do I do
now?", and two failure modes here are clinically dangerous rather than merely unhelpful:

* a drug that could not be resolved must never read like "no interactions found";
* an internal failure must say whether anything was written to the chart.

The rest enforce that internal jargon ("wrong token type", "via DrugVocabulary", raw byte
counts) stays in the log, where the ``detail`` argument puts it.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from app.exceptions import (
    AetherError,
    DocumentNotFoundError,
    NotFoundError,
    PatientNotFoundError,
    SessionExpiredError,
    TokenError,
    UnsupportedFileTypeError,
    ValidationError,
    default_message,
)
from app.services.document_service import file_too_large_message
from app.services.filetype import describe_unsupported
from tests.conftest import create_patient
from tests.test_safety import _check, _setup_patient_with_record

DOCX = b"PK\x03\x04" + b"\x00" * 64
TIFF = b"II*\x00" + b"\x00" * 64


# --------------------------------------------------------------------------- the detail channel


def test_detail_is_logged_and_never_serialized():
    err = TokenError(detail="token type 'refresh', expected 'access'")
    assert err.detail == "token type 'refresh', expected 'access'"
    assert "refresh" not in err.message


def test_default_message_uses_only_the_docstrings_first_paragraph():
    """Anything after a blank line is developer commentary, not clinician copy."""
    assert "wire contract" in (SessionExpiredError.__doc__ or "")
    assert "wire contract" not in default_message(SessionExpiredError)
    assert default_message(SessionExpiredError).startswith("Your session ended")


def test_default_message_collapses_the_docstrings_line_wrapping():
    message = default_message(UnsupportedFileTypeError)
    assert "\n" not in message
    assert "  " not in message


def test_default_message_falls_back_for_an_undocumented_error():
    class Undocumented(AetherError):
        pass

    assert Undocumented().message == "Error"


@pytest.mark.asyncio
async def test_domain_error_detail_reaches_the_log_with_the_request_id(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.main"):
        resp = await client.get("/api/v1/patients", headers={"Authorization": "Bearer nonsense"})
    assert resp.status_code == 401
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "jwt rejected" in logged
    assert resp.headers["X-Request-Id"] in logged
    # ...and none of it reached the clinician.
    assert "jwt" not in resp.json()["message"].lower()


# ------------------------------------------------------------------------------------- auth


@pytest.mark.asyncio
async def test_signed_out_request_says_to_sign_in_not_which_header_was_missing(client):
    body = (await client.get("/api/v1/patients")).json()
    assert body["message"] == "You are not signed in. Sign in to open patient records."
    assert "Bearer" not in body["message"]
    assert "token" not in body["message"].lower()


@pytest.mark.asyncio
async def test_expired_access_token_says_the_session_expired(client):
    """An expired token is the single most common 401 and has a specific remedy."""
    from datetime import UTC, datetime, timedelta

    import jwt

    from app.config import settings

    expired = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "type": "access",
            "exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp()),
        },
        settings.app_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    resp = await client.get("/api/v1/patients", headers={"Authorization": f"Bearer {expired}"})
    body = resp.json()
    assert body["message"] == "Your sign-in session has expired. Sign in again to continue."


def test_idle_session_message_reassures_that_saved_work_survived():
    message = SessionExpiredError().message
    assert "inactivity" in message
    assert "nothing you saved to a chart was lost" in message


# ------------------------------------------------------------------------------- not found


def test_patient_and_document_misses_say_where_to_go_next():
    assert "patient list" in PatientNotFoundError().message
    assert "Upload the file again" in DocumentNotFoundError().message
    # No bare "not found." restatements of the status code.
    for err in (PatientNotFoundError(), DocumentNotFoundError(), NotFoundError()):
        assert len(err.message.split()) >= 8, err.message


@pytest.mark.asyncio
async def test_a_malformed_id_in_the_url_blames_the_link_not_the_record(auth_client):
    """404 from an unparseable path uuid: the chart was never real, so don't send them hunting."""
    body = (await auth_client.get("/api/v1/patients/3")).json()
    assert body["code"] == "not_found"
    assert "truncated" in body["message"]
    assert "patient list" in body["message"]


# --------------------------------------------------------------------------------- uploads


def test_too_large_message_is_in_megabytes_with_a_way_out():
    message = file_too_large_message(20 * 1024 * 1024, 41_300_000)
    assert "39.4 MB" in message
    assert "20 MB" in message
    assert "separate files" in message
    assert "20971520" not in message


def test_too_large_message_omits_the_actual_size_on_the_streaming_path():
    message = file_too_large_message(20 * 1024 * 1024)
    assert "over the 20 MB limit" in message
    assert "separate files" in message


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (DOCX, "Word/Excel document"),
        (b"\xd0\xcf\x11\xe0" + b"\x00" * 32, ".doc/.xls"),
        (TIFF, "TIFF"),
        (b"MM\x00*" + b"\x00" * 32, "TIFF"),
        (b"GIF89a" + b"\x00" * 32, "GIF"),
        (b"BM" + b"\x00" * 32, "BMP"),
        (b"\x1f\x8b" + b"\x00" * 32, "gzip"),
        (b"%!PS-Adobe" + b"\x00" * 32, "PostScript"),
        (b"{\\rtf1" + b"\x00" * 32, "rich-text"),
    ],
)
def test_unsupported_upload_names_the_format_it_actually_got(data, expected):
    described = describe_unsupported(data)
    assert expected in described
    assert "PDF or JPEG" in described


def test_unsupported_upload_distinguishes_empty_from_truncated_from_unknown():
    assert describe_unsupported(b"") == "The file was empty."
    assert "truncated" in describe_unsupported(b"abc")
    unknown = describe_unsupported(b"\x07\x07\x07\x07" + b"\x00" * 32)
    assert "did not match any supported format" in unknown
    # Never assert a format that was not positively identified.
    assert "looks like" not in unknown


@pytest.mark.asyncio
async def test_uploading_a_tiff_scan_is_told_to_export_it_as_pdf(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("scan.tiff", TIFF, "image/tiff")},
    )
    assert resp.status_code == 422
    message = resp.json()["message"]
    assert "TIFF" in message
    assert "hospital scanners" in message
    assert "Re-save or export it as a PDF" in message


# ---------------------------------------------------------------------------- drug safety


@pytest.mark.asyncio
async def test_an_unresolvable_drug_must_not_read_like_a_clean_safety_check(auth_client):
    """The dangerous reading of this error is "checked, nothing found". Say the opposite."""
    patient = await create_patient(auth_client)
    resp = await _check(auth_client, patient["id"], drug_name="Zzyxtroban")
    assert resp.status_code == 422
    message = resp.json()["message"]
    assert "Zzyxtroban" in message
    assert "no safety check" in message
    assert "not the same as" in message
    assert "generic (INN) name" in message
    # The internal vocabulary pipeline is not the clinician's problem.
    assert "DrugVocabulary" not in message


@pytest.mark.asyncio
async def test_overriding_an_advisory_flag_explains_that_no_override_is_needed(auth_client):
    pid = await _setup_patient_with_record(auth_client)
    check = await _check(auth_client, pid, drug_reference_id="MET-500")
    advisory = next(f for f in check.json()["flags"] if not f["is_hard_block"])

    resp = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/override",
        json={
            "drug_safety_check_id": advisory["id"],
            "reasoning": "clinically indicated after discussion with the patient",
        },
    )
    assert resp.status_code == 422
    message = resp.json()["message"]
    assert "advisory" in message
    assert "encounter note" in message
    assert "hard block" in message


# ------------------------------------------------------------------- validation error bodies


def test_validation_error_default_points_at_the_fields():
    assert "highlighted fields" in ValidationError().message
