"""File storage abstraction. Local filesystem for dev; S3/MinIO-ready interface.

Files are stored under a content-addressed path (sha256 prefix) so identical bytes share
a path and deduplication is trivial. Storage paths are never public URLs.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from app.config import settings
from app.exceptions import DocumentNotFoundError

logger = logging.getLogger(__name__)

# Only a plain alphanumeric extension is carried over from the uploaded name; anything else
# (separators, dot segments, control characters, a 400-character "extension") is dropped so a
# hostile filename can never influence where the bytes land.
_SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,10}$")


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_suffix(file_name: str) -> str:
    """The uploaded file's extension if it is unambiguously safe, else ``""``."""
    suffix = Path(file_name).suffix
    return suffix if _SAFE_SUFFIX.match(suffix) else ""


class LocalStorage:
    def __init__(self, base_dir: str | None = None) -> None:
        self.base = Path(base_dir or settings.local_storage_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path_for(self, patient_id: str, sha256: str, file_name: str) -> Path:
        # storage/<patient>/<aa>/<sha256><ext> — every path component is derived from a
        # UUID or a hex digest, so the tree shape is not user-controllable.
        shard = sha256[:2]
        return self.base / patient_id / shard / f"{sha256}{safe_suffix(file_name)}"

    def _confined(self, storage_path: str) -> Path:
        """Resolve ``storage_path`` and refuse anything that lands outside the storage root.

        Defence in depth, not a fix for a live hole: today every ``documents.storage_path``
        was produced by :meth:`_path_for` from a UUID and a hex digest, so none of them can
        point outside. But ``read`` is reached from an authenticated download endpoint that
        returns the bytes verbatim, which makes it a ready-made confused deputy the moment
        that column stops being trustworthy — a restored/migrated backup, a hand-edited row,
        an injection elsewhere, or simply a future backend that writes paths differently.
        Confining here means the blast radius of any of those is a 404, not arbitrary file
        disclosure over HTTP.

        ``resolve()`` also collapses symlinks, so a link planted inside the tree that points
        at ``/etc/passwd`` fails the check too.
        """
        resolved = Path(storage_path).resolve()
        if not resolved.is_relative_to(self.base.resolve()):
            logger.error(
                "Refused a document read outside the storage root (path=%r) — the "
                "documents.storage_path column is not trustworthy.",
                storage_path,
            )
            raise DocumentNotFoundError(detail=f"storage_path escapes the storage root: {resolved}")
        return resolved

    def write(self, patient_id: str, sha256: str, file_name: str, data: bytes) -> str:
        path = self._path_for(patient_id, sha256, file_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        return str(path)

    def read(self, storage_path: str) -> bytes:
        """Return the stored bytes for a document.

        A document row whose file has gone missing (pruned volume, half-restored backup,
        storage mounted elsewhere) used to surface as a generic 500 "please try again" — advice
        that can never work, on a screen where the clinician is waiting for the original scan.
        It is reported as an unavailable document instead, and logged at error level because the
        record and the bytes have diverged and that is an operator's problem, not the user's.
        """
        path = self._confined(storage_path)
        try:
            return path.read_bytes()
        except OSError as exc:
            logger.error(
                "Document row points at unreadable storage (path=%r): %s: %s",
                storage_path,
                type(exc).__name__,
                exc,
            )
            raise DocumentNotFoundError(
                "The original file for this document could not be read from storage. Its "
                "extracted details are still in the chart; ask your administrator to check "
                "document storage, or upload the file again.",
                detail=f"{type(exc).__name__} reading {storage_path!r}",
            ) from exc

    def exists(self, storage_path: str) -> bool:
        try:
            return self._confined(storage_path).exists()
        except DocumentNotFoundError:
            return False


def get_storage() -> LocalStorage:
    # S3 backend is wired the same way for production; local for Phase 1 dev.
    return LocalStorage()
