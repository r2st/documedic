"""Document blob I/O must not run on the event loop.

The same defect the bcrypt off-load fixed in ``core.security`` (see
``test_password_hashing_off_loop``), with blocking I/O in place of burnt CPU. ``LocalStorage``
moves up to ``settings.max_upload_bytes`` (20 MB) of bytes to and from a disk that in production
is a network volume, and both of its real callers are ``async def``: the upload endpoint and the
original-scan download. Called straight from there, the whole transfer happens with the event
loop held, so a single clinician downloading a large scan stalls every other request the worker
is serving -- other charts, the SSE reasoning streams, and the ``/health/ready`` probe that tells
the load balancer this instance is alive.

The upload path hashed on the loop too, twice over: ``compute_sha256`` runs over the full 20 MB
to key deduplication, and again on the rejection path when the magic bytes are unrecognised.

These tests assert the property rather than a duration, because a duration is a flaky test on a
loaded machine: the blocking call has to end up on a thread that is not the loop's, and the loop
has to stay free while it runs. The sync primitives are deliberately kept -- the ingestion
scripts and tests that call them have no loop to protect -- so the tests below check the callers
that do have one, not merely that the wrappers exist.
"""

from __future__ import annotations

import asyncio
import threading
import uuid

import pytest

from app.exceptions import DocumentNotFoundError
from app.services import storage as storage_module
from app.services.storage import LocalStorage, compute_sha256, compute_sha256_async
from tests.conftest import create_patient

SHA = "b" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"MEDICATIONS:\nCrocin 650mg\n"
GIBBERISH = b"\x17\x93\xa4" + b"not a format we know" * 8


@pytest.fixture
def blob_threads(monkeypatch):
    """Record the thread every blocking storage primitive runs on.

    Patched at the primitives themselves rather than at the ``*_async`` wrappers. That is the
    difference between a test of the fix and a test of an import statement: a caller that went
    back to ``self.storage.write(...)`` would bypass a patched wrapper entirely and the test
    would pass for the wrong reason. Everything reaches these eventually.
    """
    threads: dict[str, list[str]] = {"write": [], "read": [], "exists": [], "sha256": []}

    real_write = LocalStorage.write
    real_read = LocalStorage.read
    real_exists = LocalStorage.exists
    real_sha = storage_module.compute_sha256

    def _write(self, patient_id, sha256, file_name, data):
        threads["write"].append(threading.current_thread().name)
        return real_write(self, patient_id, sha256, file_name, data)

    def _read(self, storage_path):
        threads["read"].append(threading.current_thread().name)
        return real_read(self, storage_path)

    def _exists(self, storage_path):
        threads["exists"].append(threading.current_thread().name)
        return real_exists(self, storage_path)

    def _sha256(data):
        threads["sha256"].append(threading.current_thread().name)
        return real_sha(data)

    monkeypatch.setattr(LocalStorage, "write", _write)
    monkeypatch.setattr(LocalStorage, "read", _read)
    monkeypatch.setattr(LocalStorage, "exists", _exists)
    monkeypatch.setattr(storage_module, "compute_sha256", _sha256)
    return threads


# --- the wrappers themselves ----------------------------------------------------------------


async def test_the_async_wrappers_return_what_the_sync_ones_do(tmp_path):
    """Off-loading may not change a value: a stored path and its bytes have to round-trip."""
    storage = LocalStorage(str(tmp_path))
    patient_id = str(uuid.uuid4())

    path = await storage.write_async(patient_id, SHA, "scan.png", PNG)

    assert path == storage.write(patient_id, SHA, "scan.png", PNG)
    assert await storage.read_async(path) == PNG
    assert await storage.exists_async(path) is True


async def test_the_async_hash_is_the_same_hash():
    assert await compute_sha256_async(PNG) == compute_sha256(PNG)


async def test_the_async_read_still_fails_the_same_way_on_a_missing_file(tmp_path):
    """A document row whose bytes are gone is a 404, not a 500 -- unchanged by the off-load."""
    storage = LocalStorage(str(tmp_path))

    with pytest.raises(DocumentNotFoundError):
        await storage.read_async(str(tmp_path / "nothing-here.png"))


async def test_the_async_read_still_refuses_a_path_outside_the_storage_root(tmp_path):
    """Confinement is a security property and has to survive being moved to a worker thread."""
    storage = LocalStorage(str(tmp_path / "root"))

    with pytest.raises(DocumentNotFoundError):
        await storage.read_async("/etc/passwd")

    assert await storage.exists_async("/etc/passwd") is False


async def test_the_wrappers_leave_the_event_loop_thread(tmp_path, blob_threads):
    storage = LocalStorage(str(tmp_path))
    loop_thread = threading.current_thread().name

    path = await storage.write_async(str(uuid.uuid4()), SHA, "scan.png", PNG)
    await storage.read_async(path)
    await storage.exists_async(path)
    await compute_sha256_async(PNG)

    for name, seen in blob_threads.items():
        assert seen, f"{name} was never called"
        assert all(t != loop_thread for t in seen), (name, seen)


async def test_the_loop_keeps_running_while_a_large_file_is_written_and_read(tmp_path):
    """The point of the whole change, stated as behaviour rather than as a thread name.

    A few megabytes is well inside the 20 MB upload cap and enough to occupy the disk for a
    measurable stretch. If the transfer ran on the loop, nothing else scheduled on it could
    advance until the transfer finished.
    """
    storage = LocalStorage(str(tmp_path))
    payload = b"\x89PNG\r\n\x1a\n" + bytes(4 * 1024 * 1024)
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    ticker = asyncio.create_task(tick())
    path = await storage.write_async(str(uuid.uuid4()), SHA, "big.png", payload)
    assert await storage.read_async(path) == payload
    ticker.cancel()

    assert ticks > 0, "the event loop made no progress while the blob moved"


# --- the callers ----------------------------------------------------------------------------


async def test_upload_does_not_write_or_hash_on_the_event_loop(auth_client, blob_threads):
    loop_thread = threading.current_thread().name
    patient = await create_patient(auth_client)

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("scan.png", PNG, "application/octet-stream")},
    )

    assert resp.status_code == 201, resp.text
    assert blob_threads["write"], "the upload never wrote the bytes to storage"
    assert blob_threads["sha256"], "the upload never hashed the bytes"
    assert all(t != loop_thread for t in blob_threads["write"]), blob_threads["write"]
    assert all(t != loop_thread for t in blob_threads["sha256"]), blob_threads["sha256"]


async def test_a_rejected_upload_does_not_hash_on_the_event_loop(auth_client, blob_threads):
    """The unsupported-file-type branch hashes the whole rejected upload for its log line.

    Nothing is stored, so this is the one path where the hash is the *only* work done with the
    bytes -- and it is reachable by anyone who can authenticate, with a 20 MB body, repeatedly.
    """
    loop_thread = threading.current_thread().name
    patient = await create_patient(auth_client)

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("mystery.bin", GIBBERISH, "application/octet-stream")},
    )

    assert resp.json()["code"] == "unsupported_file_type", resp.text
    assert blob_threads["sha256"], "the rejection path never hashed the bytes"
    assert all(t != loop_thread for t in blob_threads["sha256"]), blob_threads["sha256"]
    assert not blob_threads["write"], "a rejected upload must not reach storage"


async def test_download_does_not_read_on_the_event_loop(auth_client, blob_threads):
    patient = await create_patient(auth_client)
    up = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("scan.png", PNG, "application/octet-stream")},
    )
    assert up.status_code == 201, up.text
    loop_thread = threading.current_thread().name
    blob_threads["read"].clear()

    resp = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{up.json()['id']}/file"
    )

    assert resp.status_code == 200
    assert resp.content == PNG
    assert blob_threads["read"], "the download never read the bytes back"
    assert all(t != loop_thread for t in blob_threads["read"]), blob_threads["read"]
