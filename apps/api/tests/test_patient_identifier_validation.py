"""Patient identifying fields: what must be refused, what is quietly normalised, and why.

``min_length=1`` on ``full_name`` let through "   ", "\\t\\n", and a lone control character —
each of which created a real chart, with consent recorded and documents attachable, that no
clinician can pick out of a patient list or reach by search. Names and phones are encrypted at
rest, so search is a substring match over the decrypted value (``PatientService.list``); a chart
whose only human-readable handle is blank or oddly spaced is effectively lost.

The split this file pins: a value that is recoverable is cleaned (a doubled space, a pasted
control character), and a value that leaves nothing behind, or that is not a phone number at
all, is refused. Refusing the recoverable cases would stop chart creation mid-consultation over
something the clinician cannot see and did not do.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.schemas.patient import PatientCreate, PatientUpdate
from tests.conftest import create_patient


def _create(**overrides) -> PatientCreate:
    payload = {"full_name": "Ramesh Kumar", "consent_given": True}
    payload.update(overrides)
    return PatientCreate(**payload)


# --- full_name: refused when it leaves nothing ----------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "   ",
        "\t\n",
        "\x00",
        "\x07\x1b",  # BEL + ESC, the shape of a bad paste
        " \t \n ",
    ],
)
def test_a_name_that_cleans_to_nothing_is_refused(name):
    with pytest.raises(PydanticValidationError, match="full_name cannot be blank"):
        _create(full_name=name)


def test_the_refusal_says_why_it_matters():
    """The message has to be actionable: "invalid" would leave the clinician retyping spaces."""
    with pytest.raises(PydanticValidationError) as caught:
        _create(full_name="   ")
    assert "patient list" in str(caught.value)


def test_an_update_cannot_blank_a_name_either():
    """An edit that empties the name produces exactly the chart create now refuses to make."""
    with pytest.raises(PydanticValidationError, match="full_name cannot be blank"):
        PatientUpdate(full_name="  ")


# --- full_name: cleaned when it is recoverable ------------------------------------------------


@pytest.mark.parametrize(
    ("supplied", "stored"),
    [
        ("  Ramesh Kumar  ", "Ramesh Kumar"),
        ("Ramesh  Kumar", "Ramesh Kumar"),  # doubled space breaks substring search
        ("Ramesh\tKumar", "Ramesh Kumar"),
        ("Ramesh\nKumar", "Ramesh Kumar"),
        ("Ram\x00esh Kumar", "Ramesh Kumar"),
        ("Ramesh\x1b Kumar", "Ramesh Kumar"),
        ("   " * 100 + "R", "R"),
    ],
)
def test_a_recoverable_name_is_cleaned_not_refused(supplied, stored):
    assert _create(full_name=supplied).full_name == stored


def test_a_name_in_a_non_latin_script_survives_cleaning():
    """This product's market writes names in Devanagari, Tamil and Bengali.

    The control-character strip works on code points, not bytes, so it must not touch these.
    A cleaner that mangled them would be worse than the bug it fixed.
    """
    for name in ("रमेश कुमार", "ரமேஷ் குமார்", "রমেশ কুমার", "José Ángel"):
        assert _create(full_name=name).full_name == name


@pytest.mark.asyncio
async def test_a_doubled_space_does_not_hide_the_chart(auth_client):
    """The point of collapsing whitespace, asserted through the search it exists for.

    Stored as typed, "Ramesh  Kumar" is invisible to a search for "Ramesh Kumar" — and since the
    column is encrypted, substring matching on the decrypted value is the only search there is.
    """
    patient = await create_patient(auth_client, full_name="Ramesh  Kumar")
    assert patient["full_name"] == "Ramesh Kumar"

    found = await auth_client.post("/api/v1/patients/search", json={"search": "Ramesh Kumar"})
    assert found.status_code == 200, found.text
    assert any(p["id"] == patient["id"] for p in found.json()["items"])


# --- phone -----------------------------------------------------------------------------------


@pytest.mark.parametrize("phone", ["++++--()", "( ) - + ", "()()()", "++++++"])
def test_a_phone_with_no_digits_is_refused(phone):
    """Punctuation alone passed the character-class check and is not a phone number.

    Six of the permitted separators satisfied both the length floor and the allowed charset, so
    "++++--()" was stored and displayed as this patient's contact number.
    """
    with pytest.raises(PydanticValidationError, match="at least 6 digits"):
        _create(phone=phone)


@pytest.mark.parametrize("phone", ["+91 123", "1 2 3 4", "(0) 1234"])
def test_a_phone_with_too_few_digits_is_refused(phone):
    with pytest.raises(PydanticValidationError, match="at least 6 digits"):
        _create(phone=phone)


def test_a_whitespace_only_phone_becomes_absent_rather_than_stored():
    """It is an empty field. Stored verbatim it reads as a number the practice has on file."""
    assert _create(phone="      ").phone is None
    assert _create(phone="").phone is None


@pytest.mark.parametrize(
    ("supplied", "stored"),
    [
        ("  9876543210  ", "9876543210"),
        ("+91 98765 43210", "+91 98765 43210"),
        ("(022) 2345-6789", "(022) 2345-6789"),
        ("9876543210\x00", "9876543210"),
        ("98765  43210", "98765 43210"),
    ],
)
def test_a_real_phone_is_kept_and_trimmed(supplied, stored):
    assert _create(phone=supplied).phone == stored


@pytest.mark.parametrize("phone", ["not-a-number!!", "12345", "  -  -  ", "+91-98765-43210x"])
def test_the_pre_existing_charset_and_length_rule_still_applies(phone):
    """Unchanged, and checked first — the digit floor is an addition beside it, not a rewrite.

    "12345" and "  -  -  " land here rather than on the digit floor: both are shorter than the
    six-character minimum, so the charset rule rejects them before the digit count is counted.
    """
    with pytest.raises(PydanticValidationError, match="digits, spaces"):
        _create(phone=phone)


# --- address_text and notes: cleaned, never refused, never trimmed ---------------------------


def test_free_text_keeps_its_line_structure():
    """An address has lines and notes are prose, so newlines and tabs are content here."""
    address = "12 MG Road\nIndiranagar\nBengaluru 560038"
    assert _create(address_text=address).address_text == address
    notes = "Hx:\n\t- HTN since 2019\n\t- T2DM since 2021"
    assert _create(notes=notes).notes == notes


def test_free_text_loses_a_nul_but_nothing_else():
    """U+0000 cannot be stored in a PostgreSQL text column at all.

    SQLite accepts it, which is why the suite never caught this: on Postgres the insert raises
    at flush and rolls back everything else the request was writing.
    """
    assert _create(notes="before\x00after").notes == "beforeafter"
    assert _create(address_text="12 MG\x00 Road").address_text == "12 MG Road"
    # ...and a NUL is the whole of what goes: the surrounding text is untouched, including
    # the double space an identifier field would have collapsed.
    assert _create(notes="a\x00b  c").notes == "ab  c"


def test_crlf_line_endings_keep_their_breaks():
    """A Windows-pasted address must not lose the newline along with the carriage return."""
    assert _create(address_text="Line one\r\nLine two").address_text == "Line one\nLine two"


def test_free_text_is_not_trimmed_or_collapsed():
    """Clinical free text is the clinician's own record and not the API's to tidy."""
    notes = "  spaced   out  \n\n  deliberately  "
    assert _create(notes=notes).notes == notes


# --- date_of_birth ----------------------------------------------------------------------------


def test_the_age_bound_counts_elapsed_years_not_year_numbers():
    """Subtracting year numbers disagreed with real age, and rejected a valid chart.

    Anyone born in a calendar year 131 back scored 131 whether or not their birthday had come
    round, so a patient who is genuinely 130 -- born late in that year -- had a correct date of
    birth refused. Elapsed years is also how age is computed everywhere it is used (eGFR, renal
    dosing thresholds), so the validator now agrees with the arithmetic downstream of it.
    """
    today = date.today()

    # 130 today: allowed, that is the boundary the message names.
    assert _create(date_of_birth=today.replace(year=today.year - 130)).date_of_birth is not None

    # Born a year and a day earlier -- genuinely 131 -- is over the bound.
    with pytest.raises(PydanticValidationError, match="over 130 years"):
        _create(date_of_birth=today.replace(year=today.year - 131) - timedelta(days=1))

    # The case the old arithmetic got wrong: 131 calendar years back, but the birthday has not
    # happened yet, so the patient is 130 and the chart must be accepted.
    not_yet_had_birthday = today.replace(year=today.year - 131) + timedelta(days=1)
    assert _create(date_of_birth=not_yet_had_birthday).date_of_birth == not_yet_had_birthday


def test_a_future_dob_is_still_refused():
    with pytest.raises(PydanticValidationError, match="cannot be in the future"):
        _create(date_of_birth=date.today() + timedelta(days=1))


def test_todays_date_is_accepted_as_a_newborns_dob():
    assert _create(date_of_birth=date.today()).date_of_birth == date.today()


# --- through the API --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_blank_name_is_a_422_not_a_created_chart(auth_client):
    """The whole point: no chart exists afterwards for anyone to have to clean up.

    Asserted by the patient list, not just the status code — a 422 that still committed the row
    would look identical from the response alone.
    """
    before = await auth_client.post("/api/v1/patients/search", json={})
    resp = await auth_client.post(
        "/api/v1/patients", json={"full_name": "   ", "consent_given": True}
    )
    assert resp.status_code == 422, resp.text

    after = await auth_client.post("/api/v1/patients/search", json={})
    assert len(after.json()["items"]) == len(before.json()["items"])


@pytest.mark.asyncio
async def test_the_422_does_not_echo_the_submitted_body_back(auth_client):
    """A validation failure on a patient create must not reflect the chart's PII.

    ``main._safe_validation_errors`` strips the rejected input; this pins that the new
    validators did not reintroduce it by putting the value in the message. ``notes`` is
    clinical history and is encrypted at rest precisely so it does not travel in the clear.
    """
    resp = await auth_client.post(
        "/api/v1/patients",
        json={
            "full_name": "   ",
            "phone": "9876543210",
            "notes": "HIV positive since 2019",
            "address_text": "12 MG Road, Bengaluru",
            "consent_given": True,
        },
    )
    assert resp.status_code == 422
    body = resp.text
    for secret in ("9876543210", "HIV positive", "MG Road"):
        assert secret not in body, f"{secret!r} leaked into the validation error"


@pytest.mark.asyncio
async def test_an_update_that_blanks_the_name_leaves_the_chart_intact(auth_client):
    patient = await create_patient(auth_client, full_name="Ramesh Kumar")
    resp = await auth_client.patch(f"/api/v1/patients/{patient['id']}", json={"full_name": "  \t "})
    assert resp.status_code == 422, resp.text

    unchanged = await auth_client.get(f"/api/v1/patients/{patient['id']}")
    assert unchanged.json()["full_name"] == "Ramesh Kumar"
