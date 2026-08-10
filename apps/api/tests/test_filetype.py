"""Magic-byte file-type sniffing — the upload path never trusts a client Content-Type."""

from __future__ import annotations

import pytest

from app.services.filetype import SUPPORTED, sniff_file_type

PAD = b"\x00" * 32


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"%PDF-1.7\n" + PAD, "pdf"),
        (b"\xff\xd8\xff\xe0" + PAD, "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + PAD, "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBP" + PAD, "image/webp"),
        (b"\x00\x00\x00\x18ftypheic" + PAD, "image/heic"),
        # HEIF and the other HEIC-family brands all normalise to the one enum value.
        (b"\x00\x00\x00\x18ftypheif" + PAD, "image/heic"),
        (b"\x00\x00\x00\x18ftypheix" + PAD, "image/heic"),
        (b"\x00\x00\x00\x18ftypmif1" + PAD, "image/heic"),
    ],
)
def test_recognised_signatures(data, expected):
    assert sniff_file_type(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"short",
        b"\x00" * 11,  # one byte under the minimum header length
        b"GIF89a" + PAD,  # a real image format we deliberately do not accept
        b"MZ\x90\x00" + PAD,  # Windows executable
        b"\x7fELF\x02\x01\x01\x00" + PAD,  # Linux executable
        b"PK\x03\x04" + PAD,  # zip/docx
        b"<html><body>hello</body></html>" + PAD,
    ],
)
def test_rejected_or_unknown_content(data):
    assert sniff_file_type(data) is None


def test_a_renamed_executable_is_not_accepted_as_a_pdf():
    """Extension and Content-Type are attacker-controlled; only the bytes decide."""
    assert sniff_file_type(b"MZ\x90\x00" + b"%PDF-" + PAD) is None


def test_pdf_signature_must_be_at_the_start():
    assert sniff_file_type(b"prefix%PDF-1.4" + PAD) is None


def test_riff_without_the_webp_brand_is_rejected():
    """RIFF also fronts WAV/AVI — the brand at offset 8 is what makes it an image."""
    assert sniff_file_type(b"RIFF\x00\x00\x00\x00WAVE" + PAD) is None


def test_ftyp_with_an_unknown_brand_is_rejected():
    assert sniff_file_type(b"\x00\x00\x00\x18ftypmp42" + PAD) is None


def test_every_detected_type_is_declared_supported():
    """SUPPORTED drives the API's documented accept-list; keep the two in step."""
    samples = [
        b"%PDF-1.7\n",
        b"\xff\xd8\xff\xe0",
        b"\x89PNG\r\n\x1a\n",
        b"RIFF\x00\x00\x00\x00WEBP",
        b"\x00\x00\x00\x18ftypheic",
    ]
    detected = {sniff_file_type(s + PAD) for s in samples}
    assert detected == set(SUPPORTED)
