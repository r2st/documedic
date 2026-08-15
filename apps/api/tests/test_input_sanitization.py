"""Control-character cleaning, applied to every clinician-writable text field — not just two.

The rules were right and well tested. They lived in ``app.schemas.patient`` and covered
``full_name``, ``phone``, ``address_text`` and ``notes``. Every *other* free-text field a
clinician can write took the string exactly as sent: the presenting complaint that opens a
reasoning run, the answers to the Triage agent's questions, the reason recorded against a
clinical decision, the documented justification for overriding a hard block, and the corrected
drug names and doses submitted from the extraction review screen.

The sharp edge in all of them is U+0000. PostgreSQL cannot store it in a text column: asyncpg
raises at flush and the whole request rolls back. For extraction approval that means the
clinician loses an entire reviewed document to a 500; for a hard-block override it means the
one audited path past an allergy block dies with no record of the attempt. SQLite accepts NUL
silently, which is why 3,700 tests never saw it — so these tests assert at the schema
boundary, where the answer is the same on either database.

The second edge is the C1 range. Those characters carry terminal escapes, and these fields are
interpolated into agent prompts, streamed to the Reasoning Theatre, quoted back in
clinician-facing error copy, and written to logs an operator reads in a terminal.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.text_sanitize import clean_free_text, clean_identifier
from app.schemas.document import ExtractionApproval, FieldCorrection
from app.schemas.patient import PatientCreate, PatientSearchRequest
from app.schemas.reasoning import DecisionRequest, IntakeAnswerIn, StartReasoningRequest
from app.schemas.safety import DrugSafetyOverrideRequest, SafetyCheckRequest
from tests.conftest import create_patient

NUL = "\x00"
ANSI_CLEAR = "\x1b[2J"


# --- The helpers themselves -------------------------------------------------------------------


def test_clean_identifier_removes_control_characters():
    assert clean_identifier(f"Ravi{NUL} Kumar{ANSI_CLEAR}") == "Ravi Kumar[2J"


def test_clean_identifier_collapses_whitespace_runs():
    """The stored name has its runs collapsed, and the values that go through this are matched
    against, not only displayed."""
    assert clean_identifier("Ramesh   Kumar  ") == "Ramesh Kumar"


def test_clean_identifier_keeps_a_tab_separating_two_words():
    """Deleting the whole control range would join "Ramesh\\tKumar" into one unsearchable
    token. Tab is left for the whitespace collapse to fold into a single space."""
    assert clean_identifier("Ramesh\tKumar") == "Ramesh Kumar"


def test_clean_free_text_keeps_line_structure():
    """Newlines and tabs are content in prose; an address has lines."""
    assert clean_free_text("line one\nline two\tindented") == "line one\nline two\tindented"


def test_clean_free_text_normalises_crlf():
    """CRLF first, so Windows-pasted text keeps its break rather than losing it with the CR."""
    assert clean_free_text("line one\r\nline two") == "line one\nline two"


def test_clean_free_text_removes_nul_and_c1():
    assert clean_free_text(f"note{NUL} body\x9b") == "note body"


def test_clean_free_text_leaves_ordinary_unicode_alone():
    """Devanagari, Tamil and emoji are not control characters. A cleaner that mangled them
    would break the market this product is built for."""
    text = "रवि कुमार — 5mg ✓"
    assert clean_free_text(text) == text
    assert clean_identifier(text) == text


# --- Reasoning engine ---------------------------------------------------------------------------


def test_a_presenting_complaint_is_cleaned():
    request = StartReasoningRequest(presenting_complaint=f"Chest pain{NUL} since 3 days")
    assert NUL not in request.presenting_complaint
    assert request.presenting_complaint == "Chest pain since 3 days"


def test_a_complaint_that_is_only_control_characters_is_refused():
    """``min_length=2`` counts *raw* characters, so ``"\\x00\\x01"`` cleared it and then
    cleaned down to ``""`` — an empty complaint the eight-agent panel is asked to reason
    about, which is the input the length floor exists to prevent."""
    with pytest.raises(ValidationError):
        StartReasoningRequest(presenting_complaint=f"{NUL}\x01")


def test_a_complaint_keeps_its_line_structure():
    """It is prose. A clinician pasting a multi-line history must not have it flattened."""
    request = StartReasoningRequest(presenting_complaint="Chest pain\nSince: 3 days")
    assert request.presenting_complaint == "Chest pain\nSince: 3 days"


def test_an_intake_answer_is_cleaned():
    answer = IntakeAnswerIn(
        question_id="00000000-0000-0000-0000-000000000001",
        answer_text=f"Yes{NUL}, on metformin",
    )
    assert answer.answer_text == "Yes, on metformin"


def test_an_intake_answer_of_only_control_characters_is_refused():
    with pytest.raises(ValidationError):
        IntakeAnswerIn(question_id="00000000-0000-0000-0000-000000000001", answer_text=NUL)


def test_a_decision_reason_is_cleaned():
    assert DecisionRequest(decision="accepted", reason=f"Agreed{NUL}").reason == "Agreed"


def test_a_decision_reason_of_only_control_characters_becomes_none():
    """Optional, unlike the two above — so it collapses to "no reason given" rather than
    422ing on a field the clinician was not required to fill in."""
    assert DecisionRequest(decision="accepted", reason=NUL * 4).reason is None


# --- Drug safety -----------------------------------------------------------------------------


def test_an_override_justification_is_cleaned_before_the_length_floor_is_applied():
    """Order matters. Cleaning after the check let a justification made of NULs clear the
    ten-character floor and then fail to be written at all — the override dying as a 500 with
    no record that a hard block had been walked."""
    with pytest.raises(ValidationError):
        DrugSafetyOverrideRequest(
            drug_safety_check_id="00000000-0000-0000-0000-000000000001",
            reasoning=NUL * 40,
        )


def test_a_real_override_justification_survives_cleaning():
    request = DrugSafetyOverrideRequest(
        drug_safety_check_id="00000000-0000-0000-0000-000000000001",
        reasoning=f"Documented desensitisation{NUL} completed in 2024.",
    )
    assert request.reasoning == "Documented desensitisation completed in 2024."


def test_a_queried_drug_name_is_cleaned():
    """An unresolved name is quoted back verbatim in the "could not be matched" message, so a
    C1 escape in it reaches a clinician-facing toast and every log line recording the miss."""
    assert SafetyCheckRequest(drug_name=f"Crocin{ANSI_CLEAR}").drug_name == "Crocin[2J"


def test_a_queried_drug_name_has_its_whitespace_collapsed():
    """The resolver matches on the string. "Amoxi  cillin" resolved to nothing that
    "Amoxi cillin" would have found — a silent no-match rather than a safety check."""
    assert SafetyCheckRequest(drug_name="Amoxi  cillin ").drug_name == "Amoxi cillin"


def test_a_drug_name_of_only_control_characters_is_refused_as_no_drug_at_all():
    """It cleans to empty, and the model requires one of the two identifiers."""
    with pytest.raises(ValidationError):
        SafetyCheckRequest(drug_name=NUL * 8)


# --- Extraction review: the widest path into the clinical graph -------------------------------


def test_a_corrected_field_value_is_cleaned():
    """The correction lands as a medication name, a dose, a lab marker, a condition or an
    allergen. It was the one clinician-writable path into the patient graph that took the
    string exactly as sent."""
    correction = FieldCorrection(entity_index=0, field_name="generic_name", value=f"Met{NUL}formin")
    assert correction.value == "Metformin"


def test_a_corrected_value_has_its_whitespace_collapsed():
    """These values are resolved through the DrugVocabulary, not merely stored. A doubled
    space is the difference between a drug that cross-checks against the patient's allergies
    and one that resolves to nothing."""
    correction = FieldCorrection(entity_index=0, field_name="brand_name_raw", value=" Cro  cin ")
    assert correction.value == "Cro cin"


@pytest.mark.parametrize("value", [42, 3.5, True, None])
def test_a_non_string_corrected_value_passes_through(value):
    """A numeric lab result is not text and must not be touched."""
    assert FieldCorrection(entity_index=0, field_name="value_numeric", value=value).value == value


def test_every_correction_in_an_approval_is_cleaned():
    """Applied per item, so one clean correction in the list does not vouch for the rest."""
    approval = ExtractionApproval(
        corrections=[
            {"entity_index": 0, "field_name": "generic_name", "value": "Aspirin"},
            {"entity_index": 1, "field_name": "generic_name", "value": f"War{NUL}farin"},
        ]
    )
    assert [c.value for c in approval.corrections] == ["Aspirin", "Warfarin"]


# --- Patient search ------------------------------------------------------------------------------


def test_a_search_term_is_normalised_the_way_the_stored_name_is():
    """The two are compared to each other. ``full_name`` is stored with its whitespace runs
    collapsed, so a term pasted as "Ramesh  Kumar" matched no chart at all."""
    assert PatientSearchRequest(search="Ramesh  Kumar").search == "Ramesh Kumar"


def test_a_search_term_of_only_whitespace_becomes_none():
    """Otherwise it is a substring match on " ", which matches every two-word name in the
    account."""
    assert PatientSearchRequest(search="   ").search is None


# --- Patient fields keep the behaviour they had -------------------------------------------------


def test_the_patient_rules_are_unchanged_by_the_move():
    """The helpers moved to ``app.core.text_sanitize``; the behaviour they gave the patient
    schema did not change."""
    patient = PatientCreate(
        full_name=f"Ravi{NUL}  Kumar ",
        notes=f"Line one\r\nLine{NUL} two",
        consent_given=True,
    )
    assert patient.full_name == "Ravi Kumar"
    assert patient.notes == "Line one\nLine two"


# --- End to end -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reasoning_run_starts_on_a_complaint_carrying_a_nul(auth_client):
    """The whole point, over HTTP: a complaint with a NUL in it opens a run rather than
    failing the first write of the session."""
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": f"Fever{NUL} and cough for 4 days"},
    )
    assert resp.status_code in (200, 201), resp.text
    stored = resp.json()["session"]["presenting_complaint"]
    assert stored == "Fever and cough for 4 days"


@pytest.mark.asyncio
async def test_a_chart_created_through_the_api_stores_no_control_characters(auth_client):
    resp = await auth_client.post(
        "/api/v1/patients",
        json={
            "full_name": f"Ravi{NUL} Kumar",
            "date_of_birth": "1980-01-01",
            "sex": "male",
            "consent_given": True,
            "notes": f"Seen in OPD{ANSI_CLEAR}",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["full_name"] == "Ravi Kumar"
    assert NUL not in body["notes"] and "\x1b" not in body["notes"]
