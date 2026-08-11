"""Magic-byte file-type detection. Never trust the client-supplied Content-Type alone."""

from __future__ import annotations

# Maps detected type -> our documents.file_type enum value.
SUPPORTED = ("pdf", "image/jpeg", "image/png", "image/webp", "image/heic")


def sniff_file_type(data: bytes) -> str | None:
    """Return the canonical file_type from magic bytes, or None if unsupported."""
    if len(data) < 12:
        return None
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # HEIC/HEIF: 'ftyp' box with heic/heif/heix/mif1 brand.
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heif", b"heix", b"mif1"):
        return "image/heic"
    return None


# Formats a clinician plausibly tries to upload, keyed by the magic bytes that identify them.
# The value names the format the way the person at the keyboard would recognise it and says
# what to do instead, because "unsupported file type" alone leaves them guessing which of the
# files on their desktop was the problem.
_RECOGNISED_UNSUPPORTED: tuple[tuple[bytes, str], ...] = (
    (b"PK\x03\x04", "a Word/Excel document or a ZIP archive"),
    (b"\xd0\xcf\x11\xe0", "an older Word/Excel document (.doc/.xls)"),
    (b"{\\rtf", "a rich-text document (.rtf)"),
    (b"II*\x00", "a TIFF image — common from hospital scanners"),
    (b"MM\x00*", "a TIFF image — common from hospital scanners"),
    (b"GIF8", "a GIF image"),
    (b"BM", "a BMP image"),
    (b"\x1f\x8b", "a gzip archive"),
    (b"%!PS", "a PostScript file"),
)

_CONVERT_ADVICE = " Re-save or export it as a PDF or JPEG and upload that."


def describe_unsupported(data: bytes) -> str:
    """Explain, in a clinician's terms, why these bytes could not be accepted.

    Returned as the tail of :class:`UnsupportedFileTypeError`'s message. Naming the format that
    *was* detected turns "unsupported file type" into something the uploader can act on without
    a support ticket: TIFF scans off a hospital scanner and Word documents are the two cases
    that actually show up, and neither is obvious from the file's icon.

    Falls back to a neutral sentence for bytes we cannot place — the message must never assert
    a format it has not positively identified.
    """
    if not data:
        return "The file was empty."
    if len(data) < 12:
        return "The file was too small to be a readable scan — it may be empty or truncated."
    for magic, description in _RECOGNISED_UNSUPPORTED:
        if data.startswith(magic):
            return f"This looks like {description}.{_CONVERT_ADVICE}"
    return "The file's contents did not match any supported format."
