"""Content-Disposition handling on document download.

The uploaded file name is clinician-supplied and is echoed back in a response header, which
makes it the one place an upload can influence the HTTP protocol layer rather than just the
response body. Two failure modes are covered here:

* A non-ASCII name (routine in this product's market -- scanned prescriptions named in
  Devanagari, Tamil, Bengali) must not blow up the download. Starlette latin-1 encodes header
  values, so naive interpolation raises UnicodeEncodeError and the stored document becomes
  permanently unretrievable.
* A name containing a double quote, semicolon or CR/LF must not be able to terminate the
  quoted string and append its own Content-Disposition parameters.
"""

from __future__ import annotations

import pytest

from tests.conftest import create_patient

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\nMEDICATIONS:\nGlycomet 500mg BD\n"


async def _upload_and_fetch(client, name: str, content: bytes = PNG):
    patient = await create_patient(client)
    up = await client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": (name, content, "application/octet-stream")},
    )
    assert up.status_code == 201, up.text
    resp = await client.get(f"/api/v1/patients/{patient['id']}/documents/{up.json()['id']}/file")
    return resp


@pytest.mark.asyncio
async def test_a_document_named_in_devanagari_can_still_be_downloaded(auth_client):
    """Regression: this returned 500 and the document could never be retrieved again."""
    resp = await _upload_and_fetch(auth_client, "मरीज-report.png")

    assert resp.status_code == 200
    assert resp.content == PNG
    disposition = resp.headers["content-disposition"]
    # The lossless form carries the real name; the ASCII form is a safe stand-in.
    assert "filename*=UTF-8''" in disposition
    assert "%E0%A4%AE" in disposition  # percent-encoded Devanagari 'म'
    assert disposition.encode("latin-1")  # the header is actually transmissible


@pytest.mark.asyncio
async def test_a_wholly_non_ascii_name_still_yields_a_usable_ascii_fallback(auth_client):
    """Transliteration can erase the name entirely -- the fallback must not be empty."""
    resp = await _upload_and_fetch(auth_client, "मरीज.png")

    assert resp.status_code == 200
    ascii_name = resp.headers["content-disposition"].split('filename="')[1].split('"')[0]
    assert ascii_name  # never an empty filename=""
    assert ascii_name.isascii()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile_name",
    [
        'evil".png"; download; x="y.png',  # closes the quoted string, adds a parameter
        "evil;filename=other.png",  # bare parameter separator
        "evil\r\nX-Injected: yes.png",  # header splitting
    ],
)
async def test_a_hostile_file_name_cannot_inject_header_parameters(auth_client, hostile_name):
    resp = await _upload_and_fetch(auth_client, hostile_name)

    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert "x-injected" not in {k.lower() for k in resp.headers}

    # Structural check: the header must parse as exactly `<type>; filename="..."; filename*=...`
    # -- the hostile text may survive as inert characters inside the quoted value, but it must
    # not be able to close that value or append a third parameter.
    disp_type, _, rest = disposition.partition("; ")
    assert disp_type in ("inline", "attachment")
    assert rest.count('"') == 2
    quoted_value = rest.split('"')[1]
    assert '"' not in quoted_value and ";" not in quoted_value
    tail = rest.split('"')[2]
    assert tail.startswith("; filename*=UTF-8''")
    assert ";" not in tail[2:], "a third disposition parameter was injected"


@pytest.mark.asyncio
async def test_an_image_is_served_inline_for_preview(auth_client):
    resp = await _upload_and_fetch(auth_client, "scan.png")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-disposition"].startswith("inline;")


@pytest.mark.asyncio
async def test_an_uploaded_pdf_is_never_rendered_inline_on_the_api_origin(auth_client):
    """A PDF's embedded JavaScript runs in the serving origin -- which is where this API's
    session tokens live. It has to be handed over as a download, not rendered."""
    resp = await _upload_and_fetch(auth_client, "rx.pdf", content=PDF)

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert resp.headers["x-content-type-options"] == "nosniff"
