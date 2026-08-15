"""A document nothing could be read from says so in the log, not just in its emptiness.

Both readers in the extraction pipeline answer "" when they cannot read the file, and both are
right to: a scanned prescription is a photograph wrapped in a PDF and genuinely has no text
layer, so an unreadable file and an image-only file degrade to the same value and the caller
falls through to the next reader either way.

That is exactly why they have to log. Downstream, "pypdf raised on this file" and "this file is
a scan" are the same empty string, and a document that came back with nothing chartable is
already the quietest thing this system produces — ``_run_extraction`` marks it
``needs_confirmation`` rather than ``completed`` precisely because nobody can tell from the
outside what happened to it. The log line is the only place the difference survives at all, and
the project convention (CLAUDE.md, Code Conventions) is that nothing is swallowed without one.

What must never reach the log is the document itself: these files are prescriptions and lab
reports, and a parser error message can quote the content it choked on. Both handlers go
through ``describe_exception`` for that reason, and the tests below pin it.
"""

from __future__ import annotations

import logging
import subprocess

import pytest

from app.config import settings
from app.services.extraction import pipeline


def test_an_unreadable_pdf_is_logged_rather_than_silently_empty(caplog, monkeypatch):
    """A PDF pypdf cannot parse returns '' *and* leaves a line saying why."""

    class _Boom:
        def __init__(self, *a, **k) -> None:  # noqa: ANN002, ANN003
            raise ValueError("EOF marker not found")

    monkeypatch.setitem(__import__("sys").modules, "pypdf", type("M", (), {"PdfReader": _Boom}))

    with caplog.at_level(logging.WARNING, logger=pipeline.__name__):
        assert pipeline._pdf_text(b"%PDF-1.4 truncated") == ""

    assert caplog.records, "an unreadable PDF was swallowed without a word"
    assert "falling back to OCR" in caplog.text


def test_a_readable_pdf_logs_nothing(caplog, monkeypatch):
    """The ordinary path stays quiet — the warning has to mean something when it appears."""

    class _Page:
        @staticmethod
        def extract_text() -> str:
            return "Tab. Paracetamol 500mg"

    class _Reader:
        def __init__(self, *a, **k) -> None:  # noqa: ANN002, ANN003
            self.pages = [_Page()]

    monkeypatch.setitem(__import__("sys").modules, "pypdf", type("M", (), {"PdfReader": _Reader}))

    with caplog.at_level(logging.WARNING, logger=pipeline.__name__):
        assert "Paracetamol" in pipeline._pdf_text(b"%PDF-1.4")

    assert not caplog.records


def test_the_pdf_failure_log_does_not_quote_the_document(caplog, monkeypatch):
    """A parser that echoes the content it choked on must not put it in the log.

    ``audit_logs`` is not the only place patient text can escape to — application logs are
    retained too, and a prescription line inside an exception message is patient data.
    """

    class _Leaky:
        def __init__(self, *a, **k) -> None:  # noqa: ANN002, ANN003
            raise ValueError("bad object near: Tab. Warfarin 5mg — Ramesh Kumar, DOB 1961-04-02")

    monkeypatch.setitem(__import__("sys").modules, "pypdf", type("M", (), {"PdfReader": _Leaky}))

    with caplog.at_level(logging.WARNING, logger=pipeline.__name__):
        pipeline._pdf_text(b"%PDF-1.4")

    assert "Warfarin" not in caplog.text
    assert "Ramesh Kumar" not in caplog.text
    assert "1961-04-02" not in caplog.text
    # The exception *type* is what a diagnosis needs and all it gets.
    assert "ValueError" in caplog.text


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(cmd="tesseract", timeout=60),
        OSError("tesseract binary vanished mid-run"),
    ],
    ids=["timeout", "oserror"],
)
def test_a_failed_ocr_run_is_logged(caplog, monkeypatch, failure):
    """Tesseract falling over is reported, including the 60s timeout.

    The timeout matters most: it is the branch that costs a real minute of a clinician's upload
    and used to leave nothing at all behind to explain where that minute went.
    """
    monkeypatch.setattr(settings, "tesseract_cmd", "tesseract")
    monkeypatch.setattr(pipeline.shutil, "which", lambda _: "/usr/bin/tesseract")

    def _explode(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise failure

    monkeypatch.setattr(pipeline.subprocess, "run", _explode)

    with caplog.at_level(logging.WARNING, logger=pipeline.__name__):
        assert pipeline._tesseract_text(b"\x89PNG fake", "image/png") == ""

    assert caplog.records, "OCR failed silently"
    assert "no text was recovered" in caplog.text
    assert "image/png" in caplog.text
