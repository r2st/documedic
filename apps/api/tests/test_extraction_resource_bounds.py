"""Reading a document is bounded by pages and characters, not only by the upload size.

``settings.max_upload_bytes`` bounds the *file*. It does not bound the work, and the comment on
``extraction_stall_minutes`` used to assume it did ("pypdf parsing is CPU-bound on a file capped
at max_upload_bytes"). PDF content streams are Flate-compressed, so both the page count and the
length of the extracted text are unbounded functions of a 20 MB upload: a file can declare tens
of thousands of pages, or a handful of pages whose streams decompress to hundreds of megabytes
of text, and then the deterministic parser walks every character of the result with a set of
regexes.

Where that cost lands is what makes it worth a test. ``_run_extraction`` dispatches the pipeline
with ``asyncio.to_thread``, which is the *default* executor — shared with bcrypt behind
``verify_password``, blob I/O and upload hashing, six threads on the production box. It is the
same saturation ``agents.util.llm_executor`` exists to keep a hung LLM provider out of, reached
by a different door: a few concurrent uploads of one pathological scan hold every thread and
signing in stops working.

Two properties, and the second matters as much as the first:

* **The limit stops the work**, rather than trimming the result afterwards. ``reader.pages`` is
  lazy, so a bound that filtered a fully-materialised list would have paid the whole cost first.
  Asserted by counting ``extract_text`` calls, which is the expensive operation itself.
* **Truncation is not failure.** The pages that were read still produce entities and the
  document still lands in the review queue. A limit that refused the upload would turn a long
  fax archive into a document the clinician cannot get into the chart at all.
"""

from __future__ import annotations

import logging

import pytest

from app.config import settings
from app.services.extraction.pipeline import ExtractionPipeline, _pdf_text, _truncate_text

pytestmark = pytest.mark.asyncio


class _FakePage:
    def __init__(self, text: str, counter: list[int]) -> None:
        self._text = text
        self._counter = counter

    def extract_text(self) -> str:
        self._counter[0] += 1
        return self._text


class _FakeReader:
    """Stands in for ``pypdf.PdfReader`` over a document with ``count`` identical pages.

    A real PDF cannot be used here: building one with tens of thousands of pages of text costs
    more in the test than the code path being bounded, and the assertion is about how many pages
    are *touched*, which only a counting stand-in can observe.
    """

    def __init__(self, count: int, text: str, counter: list[int]) -> None:
        self.pages = [_FakePage(text, counter) for _ in range(count)]


@pytest.fixture
def fake_pdf(monkeypatch):
    """Install a fake ``PdfReader``; returns a factory and the extract_text call counter."""
    counter = [0]

    def install(count: int, text: str = "Tab Metformin 500mg BD\n") -> list[int]:
        import pypdf

        monkeypatch.setattr(pypdf, "PdfReader", lambda _stream: _FakeReader(count, text, counter))
        return counter

    return install


async def test_only_the_page_limit_is_ever_read(fake_pdf, monkeypatch):
    monkeypatch.setattr(settings, "max_pdf_pages_extracted", 5)
    counter = fake_pdf(count=5000)

    text = _pdf_text(b"%PDF-1.4 pretend")

    assert counter[0] == 5, "pages past the limit were still decompressed"
    assert text.count("Metformin") == 5


async def test_pages_past_the_limit_are_reported_not_silently_dropped(
    fake_pdf, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "max_pdf_pages_extracted", 3)
    fake_pdf(count=40)

    with caplog.at_level(logging.WARNING):
        _pdf_text(b"%PDF-1.4 pretend")

    assert any("3 of a PDF's 40 pages" in record.getMessage() for record in caplog.records)


async def test_a_document_inside_the_limit_is_read_whole_and_says_nothing(
    fake_pdf, monkeypatch, caplog
):
    """The ordinary case: a prescription is one page and must not look like a truncated read."""
    monkeypatch.setattr(settings, "max_pdf_pages_extracted", 200)
    counter = fake_pdf(count=4)

    with caplog.at_level(logging.WARNING):
        text = _pdf_text(b"%PDF-1.4 pretend")

    assert counter[0] == 4
    assert text.count("Metformin") == 4
    assert not caplog.records


async def test_the_character_budget_stops_a_short_document_with_enormous_pages(
    fake_pdf, monkeypatch
):
    """Few pages, each decompressing to a lot of text — the other half of the bomb."""
    monkeypatch.setattr(settings, "max_pdf_pages_extracted", 200)
    monkeypatch.setattr(settings, "max_extracted_text_chars", 1000)
    counter = fake_pdf(count=50, text="x" * 900)

    text = _pdf_text(b"%PDF-1.4 pretend")

    # Two pages take it past 1000 characters, so the third is never extracted.
    assert counter[0] == 2
    assert len(text) <= 1000


async def test_truncation_leaves_a_readable_document_rather_than_a_failure(fake_pdf, monkeypatch):
    """A truncated read still parses into entities and still reaches the review queue."""
    monkeypatch.setattr(settings, "max_pdf_pages_extracted", 2)
    fake_pdf(count=500, text="Tab Metformin 500mg BD\n")

    result = ExtractionPipeline().run(b"%PDF-1.4 pretend", "pdf")

    assert result.entities, "a truncated read produced nothing at all"


async def test_ocr_output_is_bounded_too(monkeypatch):
    """Tesseract's 60s timeout bounds how long it runs, not how much it can return."""
    monkeypatch.setattr(settings, "max_extracted_text_chars", 500)
    monkeypatch.setattr(
        "app.services.extraction.pipeline._tesseract_text", lambda *_args: "y" * 100_000
    )

    text, ocr_used = ExtractionPipeline()._recover_text(b"\xff\xd8\xff", "image/jpeg", None)

    assert len(text) == 500
    assert ocr_used is True


async def test_a_plain_text_upload_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "max_extracted_text_chars", 500)

    text, _ = ExtractionPipeline()._recover_text(b"z" * 100_000, "text/plain", None)

    assert len(text) == 500


async def test_supplied_raw_text_is_bounded(monkeypatch):
    """``raw_text`` skips both readers, so it would otherwise reach the parser unbounded."""
    monkeypatch.setattr(settings, "max_extracted_text_chars", 500)

    text, _ = ExtractionPipeline()._recover_text(b"", "pdf", "w" * 100_000)

    assert len(text) == 500


async def test_truncating_says_so(monkeypatch, caplog):
    monkeypatch.setattr(settings, "max_extracted_text_chars", 10)

    with caplog.at_level(logging.WARNING):
        assert _truncate_text("a" * 50, source="pdf") == "a" * 10

    assert any("truncated" in record.getMessage() for record in caplog.records)


async def test_text_inside_the_budget_is_untouched(monkeypatch, caplog):
    monkeypatch.setattr(settings, "max_extracted_text_chars", 10)

    with caplog.at_level(logging.WARNING):
        assert _truncate_text("abc", source="pdf") == "abc"

    assert not caplog.records


async def test_an_unreadable_pdf_still_degrades_to_the_empty_string(monkeypatch):
    """The pre-existing contract: a parse failure is '' so the caller falls through to OCR."""
    import pypdf

    def _boom(_stream):
        raise ValueError("not a pdf after all")

    monkeypatch.setattr(pypdf, "PdfReader", _boom)
    assert _pdf_text(b"%PDF-1.4 broken") == ""
