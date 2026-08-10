"""Storage-path construction.

The uploaded filename is attacker-controlled. Nothing derived from it may influence where
the bytes land on disk, and it must not be persisted unbounded.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.services.document_service import MAX_FILE_NAME_CHARS
from app.services.storage import LocalStorage, compute_sha256, safe_suffix
from tests.conftest import create_patient

SHA = "a" * 64

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"MEDICATIONS:\nCrocin 650mg\n"


@pytest.mark.parametrize(
    "file_name",
    [
        "../../../../etc/passwd",
        "scan.pdf/../../../../etc/cron.d/evil",
        "scan.\\..\\..\\windows\\system32",
        "scan." + "x" * 300,
        "scan.p/df",
        "scan.\x00pdf",
        "....//....//etc",
        "scan.",
    ],
)
def test_a_hostile_filename_cannot_shape_the_storage_path(tmp_path, file_name):
    storage = LocalStorage(str(tmp_path))
    patient_id = str(uuid.uuid4())
    path = Path(storage._path_for(patient_id, SHA, file_name))

    resolved = path.resolve()
    assert resolved.is_relative_to(tmp_path.resolve()), f"{file_name} escaped the storage root"
    # Exactly <root>/<patient>/<shard>/<digest><ext>, all machine-generated.
    assert resolved.parent.parent.name == patient_id
    assert resolved.parent.name == SHA[:2]
    assert resolved.stem == SHA


def test_a_normal_extension_is_preserved(tmp_path):
    storage = LocalStorage(str(tmp_path))
    path = Path(storage._path_for(str(uuid.uuid4()), SHA, "prescription.pdf"))
    assert path.name == f"{SHA}.pdf"


@pytest.mark.parametrize("name", ["a.pdf", "a.PNG", "a.jpeg", "a.webp", "a.heic"])
def test_ordinary_document_extensions_survive_sanitisation(name):
    assert safe_suffix(name) == Path(name).suffix


@pytest.mark.parametrize("name", ["a", "a.", "a.tar.gz-../x", "a." + "z" * 11, "a./b"])
def test_unsafe_or_absent_extensions_become_empty(name):
    assert safe_suffix(name) == ""


def test_write_and_read_round_trip_under_the_storage_root(tmp_path):
    storage = LocalStorage(str(tmp_path))
    data = b"contents"
    sha = compute_sha256(data)
    written = storage.write(str(uuid.uuid4()), sha, "report.pdf", data)

    assert Path(written).resolve().is_relative_to(tmp_path.resolve())
    assert storage.exists(written)
    assert storage.read(written) == data


def test_identical_bytes_share_one_path(tmp_path):
    storage = LocalStorage(str(tmp_path))
    data = b"same bytes"
    sha = compute_sha256(data)
    patient_id = str(uuid.uuid4())

    first = storage.write(patient_id, sha, "a.pdf", data)
    second = storage.write(patient_id, sha, "a.pdf", data)
    assert first == second


@pytest.mark.asyncio
async def test_an_overlong_upload_filename_is_truncated_before_it_is_stored(auth_client):
    """Starlette rejects an absurd multipart header outright; this is the band in between,
    long enough to be unreasonable but short enough to reach the service."""
    patient = await create_patient(auth_client)
    long_name = "x" * 600 + ".png"

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": (long_name, PNG, "image/png")},
    )
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["file_name"]) == MAX_FILE_NAME_CHARS


@pytest.mark.asyncio
async def test_a_missing_upload_filename_falls_back_to_a_placeholder(db):
    """`UploadFile.filename` is optional; an empty one must not become an empty path."""
    from app.models.user import Account
    from app.schemas.patient import PatientCreate
    from app.services.document_service import DocumentService
    from app.services.patient_service import PatientService

    account = Account(email=f"noname-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = await PatientService(db).create(
        account.id, PatientCreate(full_name="No Name", sex="male", consent_given=True)
    )

    document = await DocumentService(db).upload(
        account_id=account.id, patient_id=patient.id, file_name="", data=PNG
    )
    assert document.file_name == "upload"


@pytest.mark.asyncio
async def test_a_traversal_filename_upload_still_lands_inside_the_storage_root(auth_client):
    patient = await create_patient(auth_client)

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("../../../../tmp/evil.png", PNG, "image/png")},
    )
    assert resp.status_code == 201, resp.text

    doc_id = resp.json()["id"]
    fetched = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents/{doc_id}/file")
    assert fetched.status_code == 200
