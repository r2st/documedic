"""Upload input validation: size cap, magic-byte gate, and ownership scoping."""

from __future__ import annotations

import pytest

from app.config import settings
from app.exceptions import FileTooLargeError
from app.routers.documents import _read_capped
from tests.conftest import create_patient

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _FakeUpload:
    """Minimal UploadFile stand-in that hands out fixed-size chunks and counts reads."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.read_calls = 0

    async def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if size is None or size < 0:
            chunk, self._pos = self._data[self._pos :], len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


# --- _read_capped ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_capped_returns_the_whole_body_when_within_limit():
    upload = _FakeUpload(PNG)
    assert await _read_capped(upload, 1024) == PNG


@pytest.mark.asyncio
async def test_read_capped_rejects_an_oversized_body():
    with pytest.raises(FileTooLargeError):
        await _read_capped(_FakeUpload(b"x" * 5000), 1000)


@pytest.mark.asyncio
async def test_read_capped_aborts_early_instead_of_buffering_everything():
    """The point of the chunked read: a huge body must not be fully spooled before the
    size check runs. Stop within one chunk of the limit, not at the end of the stream."""
    huge = b"x" * (64 * 1024 * 1024)  # 64 MB against a 1 MB cap
    upload = _FakeUpload(huge)
    with pytest.raises(FileTooLargeError):
        await _read_capped(upload, 1024 * 1024)
    assert upload.read_calls <= 3  # not the 64 it would take to drain the stream


@pytest.mark.asyncio
async def test_read_capped_accepts_a_body_exactly_at_the_limit():
    data = b"y" * 1000
    assert await _read_capped(_FakeUpload(data), 1000) == data


@pytest.mark.asyncio
async def test_read_capped_handles_an_empty_body():
    assert await _read_capped(_FakeUpload(b""), 1000) == b""


# --- Endpoint behaviour ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_upload_is_rejected_with_422(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 512)
    patient = await create_patient(auth_client)

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("big.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096, "image/png")},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "file_too_large"


@pytest.mark.asyncio
async def test_unsupported_content_is_rejected_by_magic_bytes(auth_client):
    """A file claiming image/png in its Content-Type but carrying executable bytes."""
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("evil.png", b"MZ\x90\x00" + b"\x00" * 64, "image/png")},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "unsupported_file_type"


@pytest.mark.asyncio
async def test_upload_to_another_accounts_patient_is_not_found(client, auth_client):
    """Cross-tenant isolation: patient ids are not a capability."""
    patient = await create_patient(auth_client)

    resp = await client.post(
        "/api/v1/auth/signup", json={"email": "other-doc@example.com", "password": "password123"}
    )
    other_token = resp.json()["access_token"]

    leak = await client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("scan.png", PNG, "image/png")},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert leak.status_code == 404


@pytest.mark.asyncio
async def test_upload_requires_authentication(client, auth_client):
    patient = await create_patient(auth_client)
    resp = await client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("scan.png", PNG, "image/png")},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 401
