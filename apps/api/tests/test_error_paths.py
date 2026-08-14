"""Coverage for error and degradation paths that the happy-path suite never reaches.

Every test here corresponds to an ``except`` handler or a ``raise`` that was uncovered — found
by cross-referencing the coverage report's missing lines against the AST of each module's
exception handlers. These are the branches that only run when something has already gone wrong,
which is exactly when correct behaviour matters most: a wrong handler turns a degraded read into
a 500, or silently drops a safety check.

Grouped by the module under test.
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import String, bindparam, select, text

from app.core.crypto import encrypt_str
from app.db.types import GUID, EncryptedDate, EncryptedString
from app.exceptions import (
    FileTooLargeError,
    NotFoundError,
    PatientNotFoundError,
    TokenError,
    ValidationError,
)
from app.models.drug_safety_check import DrugSafetyCheck
from app.models.drug_vocabulary import DrugVocabulary
from app.models.patient import Patient
from app.models.user import Account
from app.services.reasoning_service import ReasoningService, SuggestionNotFoundError
from app.services.safety_service import SafetyService
from tests.conftest import create_patient


async def _account(db) -> Account:
    account = Account(email=f"err-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    return account


async def _drug(db) -> DrugVocabulary:
    drug = DrugVocabulary(
        brand_name="Crocin",
        generic_name="Paracetamol",
        reference_id=f"ref-{uuid.uuid4().hex}",
    )
    db.add(drug)
    await db.flush()
    return drug


async def _safety_check(db, account, patient, *, is_hard_block: bool) -> DrugSafetyCheck:
    """A persisted check. `check_type`/`severity` must satisfy the table's CHECK constraints."""
    drug = await _drug(db)
    check = DrugSafetyCheck(
        patient_id=patient.id,
        account_id=account.id,
        drug_vocabulary_id=drug.id,
        check_type="allergy_conflict" if is_hard_block else "drug_interaction",
        severity="hard_block" if is_hard_block else "warning",
        is_hard_block=is_hard_block,
        summary="Allergy hard block" if is_hard_block else "Moderate interaction",
        details={},
    )
    db.add(check)
    await db.flush()
    return check


async def _patient(db, account: Account) -> Patient:
    patient = Patient(
        account_id=account.id,
        full_name="Error Path Patient",
        sex="male",
        date_of_birth=date(1970, 1, 1),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return patient


# --------------------------------------------------------------------------------------
# app/db/types.py — encrypted-column read resilience
#
# This is the live production shape right now: migration 0006 widened the patient PII columns
# and enabled encryption without re-encrypting rows written before it, so real rows are
# plaintext behind an EncryptedString column. Raising there would take down every read of an
# existing patient, so the documented policy is to degrade, not crash.
# --------------------------------------------------------------------------------------


def test_encrypted_string_returns_legacy_plaintext_unchanged():
    """A pre-encryption plaintext row reads back as-is rather than raising InvalidToken."""
    assert EncryptedString().process_result_value("Ramesh Kumar", None) == "Ramesh Kumar"


def test_encrypted_string_returns_value_written_with_a_different_key():
    """A row encrypted under a rotated key is returned raw, not raised, so the read survives."""
    # A syntactically valid Fernet token this key cannot open.
    foreign = (
        "gAAAAABm" + "A" * 60  # well-formed prefix, wrong key material
    )
    assert EncryptedString().process_result_value(foreign, None) == foreign


def test_encrypted_string_round_trips_a_real_value():
    assert EncryptedString().process_result_value(encrypt_str("Priya"), None) == "Priya"


def test_encrypted_string_passes_none_through():
    assert EncryptedString().process_result_value(None, None) is None
    assert EncryptedString().process_bind_param(None, None) is None


def test_encrypted_date_returns_none_when_the_value_cannot_be_decrypted():
    """A date is structured, so an undecryptable one degrades to None rather than a bad date."""
    assert EncryptedDate().process_result_value("1968-05-10", None) is None


def test_encrypted_date_returns_none_when_plaintext_decrypts_but_is_not_a_date():
    """Decrypts cleanly, but the payload is not an ISO date — still must not raise."""
    assert EncryptedDate().process_result_value(encrypt_str("not-a-date"), None) is None


def test_encrypted_date_round_trips_and_accepts_an_iso_string_on_write():
    column = EncryptedDate()

    def _round_trip(value):
        return column.process_result_value(column.process_bind_param(value, None), None)

    assert _round_trip(date(1968, 5, 10)) == date(1968, 5, 10)
    # A str going in is coerced, matching the model's tolerance for ISO input.
    assert _round_trip("1968-05-10") == date(1968, 5, 10)
    assert column.process_result_value(None, None) is None
    assert column.process_bind_param(None, None) is None


async def test_patient_with_legacy_plaintext_pii_still_reads_through_the_api(db, auth_client):
    """End-to-end version of the above: a plaintext row must not 500 the patient endpoints.

    Writes plaintext directly past the ORM (as rows predating migration 0006 are), then reads
    the patient back through the API.
    """
    created = await create_patient(auth_client, full_name="Legacy Row")
    patient_id = created["id"]

    # Typed bindparams matter here: `id` is a GUID column, stored as bare 32-char hex off
    # PostgreSQL, so a dashed string would silently match zero rows and the test would pass
    # against an unmodified row. `name` is bound as a plain String to bypass EncryptedString's
    # bind processor -- writing plaintext past the ORM is the whole point.
    await db.execute(
        text("UPDATE patients SET full_name = :name WHERE id = :pid").bindparams(
            bindparam("name", "Plaintext Name", type_=String()),
            bindparam("pid", uuid.UUID(patient_id), type_=GUID()),
        )
    )
    await db.commit()

    resp = await auth_client.get(f"/api/v1/patients/{patient_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["full_name"] == "Plaintext Name"


# --------------------------------------------------------------------------------------
# app/services/safety_service.py + app/routers/safety.py
# --------------------------------------------------------------------------------------


async def test_override_of_unknown_safety_check_raises_not_found(db):
    account = await _account(db)
    patient = await _patient(db, account)

    with pytest.raises(NotFoundError):
        await SafetyService(db).override_hard_block(
            account_id=account.id,
            patient_id=patient.id,
            drug_safety_check_id=uuid.uuid4(),
            reasoning="Documented clinical rationale for proceeding despite the block.",
        )


async def test_override_of_another_patients_check_raises_not_found(db):
    """The check exists, but belongs to a different patient — must not be overridable."""
    account = await _account(db)
    patient_a = await _patient(db, account)
    patient_b = await _patient(db, account)

    check = await _safety_check(db, account, patient_b, is_hard_block=True)

    with pytest.raises(NotFoundError):
        await SafetyService(db).override_hard_block(
            account_id=account.id,
            patient_id=patient_a.id,
            drug_safety_check_id=check.id,
            reasoning="Documented clinical rationale for proceeding despite the block.",
        )


async def test_override_of_a_non_hard_block_is_rejected(db):
    """Only hard blocks take the override path; a soft flag needs no documented override."""
    account = await _account(db)
    patient = await _patient(db, account)
    check = await _safety_check(db, account, patient, is_hard_block=False)

    with pytest.raises(ValidationError):
        await SafetyService(db).override_hard_block(
            account_id=account.id,
            patient_id=patient.id,
            drug_safety_check_id=check.id,
            reasoning="Documented clinical rationale for proceeding despite the block.",
        )


def test_safety_uuid_coercion_tolerates_junk():
    """Flag ids arrive from the deterministic rule engine as strings; junk degrades to None.

    A raising coercion here would drop the whole safety response — the failure mode this
    project least wants, since these are the offline allergy/interaction flags.
    """
    from app.routers.safety import _uuid
    from app.services.safety_service import _to_uuid

    valid = uuid.uuid4()
    for coerce in (_uuid, _to_uuid):
        assert coerce(str(valid)) == valid
        assert coerce("not-a-uuid") is None
        assert coerce("") is None
        assert coerce(None) is None


async def test_active_flags_for_unknown_patient_raises_not_found(db):
    account = await _account(db)
    with pytest.raises(PatientNotFoundError):
        await SafetyService(db).active_flags(account_id=account.id, patient_id=uuid.uuid4())


# --------------------------------------------------------------------------------------
# app/services/reasoning_service.py
# --------------------------------------------------------------------------------------


async def test_reasoning_start_for_unknown_patient_raises_not_found(db):
    account = await _account(db)
    with pytest.raises(PatientNotFoundError):
        await ReasoningService(db).start(account.id, uuid.uuid4(), "chest pain")


async def test_record_decision_for_unknown_suggestion_raises_not_found(db):
    account = await _account(db)
    patient = await _patient(db, account)
    session, _questions = await ReasoningService(db).start(account.id, patient.id, "chest pain")

    with pytest.raises(SuggestionNotFoundError):
        await ReasoningService(db).record_decision(
            account_id=account.id,
            session_id=session.id,
            suggestion_id=uuid.uuid4(),
            decision="accepted",
            reason=None,
        )


async def test_reasoning_failure_marks_the_session_failed_and_audits(db, monkeypatch):
    """A crash inside the agent graph must be recorded, not swallowed.

    Pins the `except Exception` in `run`: the session is flipped to `failed` with the error
    detail, an audit row is written, and the exception still propagates so the caller sees it.
    """
    from app.models.audit_log import AuditLog
    from app.services import reasoning_service as rs

    account = await _account(db)
    patient = await _patient(db, account)
    service = ReasoningService(db)
    session, _questions = await service.start(account.id, patient.id, "chest pain")

    async def _boom(state, ctx):
        raise RuntimeError("agent graph exploded")

    monkeypatch.setattr(rs.graph, "run_reasoning", _boom)

    with pytest.raises(RuntimeError, match="agent graph exploded"):
        await service.run(account.id, session.id)

    refreshed = await service.get_session(account.id, session.id)
    assert refreshed.status == "failed"
    # The exception type, not its message. Failures here surface from an LLM provider that was
    # just sent this patient's snapshot, and one that quotes the prompt back in its error would
    # otherwise land that text in a plaintext column and in audit_logs.payload — immutable and
    # never pruned. See app.core.logsafe.
    assert refreshed.error_detail == "RuntimeError"

    audit = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "reasoning_session_failed")))
        .scalars()
        .all()
    )
    assert audit, "a failed reasoning session must leave an audit trail"
    assert audit[0].payload == {"error": "RuntimeError"}


# --------------------------------------------------------------------------------------
# app/services/document_service.py
# --------------------------------------------------------------------------------------


async def test_upload_over_the_size_cap_raises_file_too_large(db, monkeypatch):
    from app.services.document_service import DocumentService

    monkeypatch.setattr("app.services.document_service.settings.max_upload_bytes", 16)
    account = await _account(db)
    patient = await _patient(db, account)

    with pytest.raises(FileTooLargeError):
        await DocumentService(db).upload(
            account_id=account.id,
            patient_id=patient.id,
            file_name="big.pdf",
            data=b"x" * 64,
        )


# --------------------------------------------------------------------------------------
# app/services/extraction/pipeline.py — OCR fallback degradation
# --------------------------------------------------------------------------------------


def test_ocr_returns_empty_when_tesseract_times_out(monkeypatch):
    """A hung Tesseract must yield empty text, not propagate and fail the whole upload."""
    from app.services.extraction import pipeline

    monkeypatch.setattr(pipeline.shutil, "which", lambda _cmd: "/usr/bin/tesseract")

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="tesseract", timeout=60)

    monkeypatch.setattr(pipeline.subprocess, "run", _timeout)
    assert pipeline._tesseract_text(b"\x89PNG fake", "image/png") == ""


def test_ocr_returns_empty_when_tesseract_cannot_be_executed(monkeypatch):
    from app.services.extraction import pipeline

    monkeypatch.setattr(pipeline.shutil, "which", lambda _cmd: "/usr/bin/tesseract")

    def _oserror(*args, **kwargs):
        raise OSError("exec format error")

    monkeypatch.setattr(pipeline.subprocess, "run", _oserror)
    assert pipeline._tesseract_text(b"\x89PNG fake", "image/png") == ""


def test_ocr_returns_empty_when_no_binary_is_installed(monkeypatch):
    from app.services.extraction import pipeline

    monkeypatch.setattr(pipeline.shutil, "which", lambda _cmd: None)
    assert pipeline._tesseract_text(b"\x89PNG fake", "image/png") == ""


# --------------------------------------------------------------------------------------
# app/agents — LLM degradation must never raise into clinical code
# --------------------------------------------------------------------------------------


def test_extract_json_rejects_a_response_with_no_json_object():
    from app.agents.llm import LLMUnavailable, _extract_json

    with pytest.raises(LLMUnavailable):
        _extract_json("I'm sorry, I can't help with that.")


def test_extract_json_recovers_an_object_wrapped_in_prose():
    """Weak models pad JSON with prose; the object is still extracted."""
    from app.agents.llm import _extract_json

    assert _extract_json('Sure! {"tier": "suggestive"} Hope that helps.') == {"tier": "suggestive"}


async def test_call_llm_returns_none_when_the_provider_is_unavailable(monkeypatch):
    """`call_llm` converts LLMUnavailable into None, which is the agents' fallback signal.

    If this raised instead, an LLM outage would surface as a 500 rather than the deterministic
    offline path the safety rules require.
    """
    from app.agents.llm import LLMUnavailable
    from app.agents.util import call_llm

    class _Client:
        def available(self) -> bool:
            return True

        def complete_json(self, system: str, user: str) -> dict:
            raise LLMUnavailable("provider down")

    class _Ctx:
        llm = _Client()
        verifier_llm = _Client()

    assert await call_llm(_Ctx(), "sys", "user") is None
    assert await call_llm(_Ctx(), "sys", "user", verifier=True) is None


async def test_call_llm_returns_none_when_no_client_is_available():
    from app.agents.util import call_llm

    class _Client:
        def available(self) -> bool:
            return False

        def complete_json(self, system: str, user: str) -> dict:  # pragma: no cover
            raise AssertionError("must not be called when unavailable")

    class _Ctx:
        llm = _Client()
        verifier_llm = _Client()

    assert await call_llm(_Ctx(), "sys", "user") is None


# --------------------------------------------------------------------------------------
# app/services/graph_service.py — eGFR derivation skips undecidable rows
# --------------------------------------------------------------------------------------


async def test_egfr_derivation_skips_a_lab_it_cannot_compute(db):
    """An implausible DOB makes age_from_dob raise; that lab is skipped, others still derive.

    The `continue` in the ValueError handler is what keeps one bad row from aborting the whole
    derived-marker pass.
    """
    from app.models.derived_marker import DerivedMarker
    from app.models.lab_result import LabResult
    from app.services.graph_service import GraphService

    account = await _account(db)
    patient = Patient(
        account_id=account.id,
        full_name="Future Born",
        sex="male",
        # Sample date precedes the DOB, so the computed age is negative -> ValueError.
        date_of_birth=date(2030, 1, 1),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()

    lab = LabResult(
        patient_id=patient.id,
        marker_name="Creatinine",
        value_numeric=1.1,
        unit="mg/dL",
        sample_date=datetime(2024, 1, 1, tzinfo=UTC),
    )
    db.add(lab)
    await db.flush()

    await GraphService(db)._compute_derived_markers(patient, [lab])
    await db.flush()

    markers = (
        (await db.execute(select(DerivedMarker).where(DerivedMarker.patient_id == patient.id)))
        .scalars()
        .all()
    )
    assert [m for m in markers if m.marker_name == "eGFR"] == []


# --------------------------------------------------------------------------------------
# Router-level error paths
# --------------------------------------------------------------------------------------


async def test_unknown_validation_run_returns_404(auth_client):
    resp = await auth_client.get(f"/api/v1/validation/runs/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_dependencies_health_reports_database_error_without_raising(app, monkeypatch):
    """The dependency probe must report a DB outage as data, not as a 500.

    A monitoring endpoint that 500s when a dependency is down tells you nothing about which
    dependency it was.
    """
    from httpx import ASGITransport, AsyncClient

    from app.db.session import get_db

    class _BrokenSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("connection refused")

    async def _broken_db():
        yield _BrokenSession()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _broken_db
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/health/dependencies")
    finally:
        if previous is not None:
            app.dependency_overrides[get_db] = previous
        else:
            del app.dependency_overrides[get_db]

    assert resp.status_code == 200
    assert resp.json()["database"] == "error"


async def test_stream_rejects_a_stream_token_presented_in_the_authorization_header(auth_client):
    """The header path demands a real `access` token; a `stream` token there is the wrong type.

    (The mirror cases — an access token in the query string, and a stream token minted for a
    different session — are covered in test_stream_token.py.)
    """
    patient = await create_patient(auth_client)
    start = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "chest pain for two days"},
    )
    assert start.status_code in (200, 201), start.text
    session_id = start.json()["session"]["id"]

    minted = await auth_client.post(f"/api/v1/reasoning/{session_id}/stream-token")
    assert minted.status_code == 200, minted.text
    stream_token = minted.json()["token"]

    resp = await auth_client.get(
        f"/api/v1/reasoning/{session_id}/stream",
        headers={"Authorization": f"Bearer {stream_token}"},
    )
    assert resp.status_code in (401, 403)


async def test_stream_rejects_a_token_with_a_malformed_subject(auth_client):
    """A correctly signed access token whose `sub` is not a UUID is a 401, not a 500."""
    from app.core.security import create_access_token

    patient = await create_patient(auth_client)
    start = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "chest pain for two days"},
    )
    session_id = start.json()["session"]["id"]

    bad = create_access_token("not-a-uuid")
    resp = await auth_client.get(
        f"/api/v1/reasoning/{session_id}/stream",
        headers={"Authorization": f"Bearer {bad}"},
    )
    assert resp.status_code in (401, 403)


# --------------------------------------------------------------------------------------
# app/services/auth_service.py — refresh-token failure modes
# --------------------------------------------------------------------------------------


async def test_refresh_with_an_unknown_token_raises_token_error(db):
    from app.services.auth_service import AuthService

    with pytest.raises(TokenError):
        await AuthService(db).refresh("not-a-real-refresh-token", ip_address=None, user_agent=None)


async def test_refresh_fails_once_the_account_is_deleted(db):
    """Rotation checks the account still exists before issuing a new pair."""
    from app.services.auth_service import AuthService

    service = AuthService(db)
    email = f"gone-{uuid.uuid4().hex}@example.com"
    # `signup` returns the Account; the refresh token only exists once a session is issued.
    account = await service.signup(
        email=email,
        password="password123",
        display_name="Gone Soon",
    )
    tokens = await service.login(email, "password123")

    account.is_deleted = True
    await db.commit()

    with pytest.raises(TokenError):
        await service.refresh(tokens.refresh_token, ip_address=None, user_agent=None)
