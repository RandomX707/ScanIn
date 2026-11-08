from __future__ import annotations

import hashlib
import io
import os
import stat
from uuid import uuid4

import pytest

from packages.ingestion.models import ScanResult
from packages.storage.local import ImmutableLocalStorage, StorageRejected


def test_untrusted_upload_stays_in_private_staging_until_clean_scan(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    staged = storage.stage(uuid4(), uuid4(), uuid4(), io.BytesIO(b"untrusted bytes"), max_bytes=1024)

    assert staged.path.exists()
    assert ".staging" in staged.path.parts
    assert stat.S_IMODE(staged.path.stat().st_mode) == 0o600
    assert not list((tmp_path / "objects").glob("**/originals/**"))

    with pytest.raises(StorageRejected, match="clean_scan_required"):
        storage.promote_original(staged, ScanResult(status="infected", sha256=staged.sha256, scanner="test"))
    assert staged.path.exists()


def test_clean_scan_promotes_immutable_tenant_prefixed_original(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    tenant, document_id, upload_id = uuid4(), uuid4(), uuid4()
    payload = b"verified original bytes"
    staged = storage.stage(tenant, document_id, upload_id, io.BytesIO(payload), max_bytes=1024)
    stored = storage.promote_original(
        staged,
        ScanResult(status="clean", sha256=staged.sha256, scanner="test-scanner"),
    )

    assert stored.tenant_id == tenant
    assert stored.sha256 == hashlib.sha256(payload).hexdigest()
    assert stored.size_bytes == len(payload)
    assert stored.document_id == document_id
    assert stored.key.startswith(f"{tenant}/originals/{document_id}/")
    assert stored.path.read_bytes() == payload
    assert stat.S_IMODE(stored.path.stat().st_mode) == 0o400
    assert not staged.path.exists()


def test_scan_attestation_must_match_staged_content(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    staged = storage.stage(uuid4(), uuid4(), uuid4(), io.BytesIO(b"one"), max_bytes=1024)

    with pytest.raises(StorageRejected, match="scan_digest_mismatch"):
        storage.promote_original(staged, ScanResult(status="clean", sha256="0" * 64, scanner="test"))
    assert staged.path.exists()


def test_upload_size_limit_removes_partial_stage(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")

    with pytest.raises(StorageRejected, match="upload_too_large"):
        storage.stage(uuid4(), uuid4(), uuid4(), io.BytesIO(b"12345"), max_bytes=4)
    assert not list((tmp_path / "objects" / ".staging").glob("**/*.part"))


def test_identical_bytes_are_not_deduplicated_across_tenants(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    document = uuid4()
    payload = b"same source bytes"
    tenant_a, tenant_b = uuid4(), uuid4()
    stored = []
    for tenant in (tenant_a, tenant_b):
        staged = storage.stage(tenant, document, uuid4(), io.BytesIO(payload), max_bytes=1024)
        stored.append(storage.promote_original(staged, ScanResult(status="clean", sha256=staged.sha256, scanner="test")))

    assert stored[0].sha256 == stored[1].sha256
    assert stored[0].key != stored[1].key
    assert stored[0].path != stored[1].path


def test_staging_rejects_symlinked_tenant_directory(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    tenant, document, upload = uuid4(), uuid4(), uuid4()
    staging_root = tmp_path / "objects" / ".staging"
    staging_root.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    (staging_root / str(tenant)).symlink_to(target, target_is_directory=True)

    with pytest.raises(StorageRejected, match="symlink_path_rejected"):
        storage.stage(tenant, document, upload, io.BytesIO(b"x"), max_bytes=10)
    assert list(target.iterdir()) == []


def test_duplicate_upload_id_cannot_overwrite_private_stage(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    tenant, document, upload = uuid4(), uuid4(), uuid4()
    first = storage.stage(tenant, document, upload, io.BytesIO(b"original"), max_bytes=100)
    with pytest.raises(StorageRejected, match="upload_id_already_staged"):
        storage.stage(tenant, document, upload, io.BytesIO(b"attacker"), max_bytes=100)
    assert first.path.read_bytes() == b"original"


def test_promoted_source_is_immutable_and_integrity_checked_on_read(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    tenant, document = uuid4(), uuid4()
    staged = storage.stage(tenant, document, uuid4(), io.BytesIO(b"source"), max_bytes=100)
    stored = storage.promote_original(staged, ScanResult(status="clean", sha256=staged.sha256, scanner="test"))
    assert storage.read_original(stored) == b"source"

    stored.path.chmod(0o600)  # Simulate an out-of-band owner-level mutation.
    stored.path.write_bytes(b"tampered")
    with pytest.raises(StorageRejected, match="stored_file_integrity_mismatch"):
        storage.read_original(stored)


def test_promoting_same_tenant_document_digest_is_idempotent(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    tenant, document = uuid4(), uuid4()
    payload = b"stable"
    first_stage = storage.stage(tenant, document, uuid4(), io.BytesIO(payload), max_bytes=100)
    first = storage.promote_original(first_stage, ScanResult(status="clean", sha256=first_stage.sha256, scanner="test"))
    second_stage = storage.stage(tenant, document, uuid4(), io.BytesIO(payload), max_bytes=100)
    second = storage.promote_original(second_stage, ScanResult(status="clean", sha256=second_stage.sha256, scanner="test"))

    assert first.key == second.key
    assert first.path == second.path
    assert not second_stage.path.exists()


def test_verified_derived_storage_is_tenant_scoped_immutable_and_idempotent(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    tenant, document, edit_set = uuid4(), uuid4(), uuid4()
    payload = b"corrected derived workbook"
    scan = ScanResult("clean", hashlib.sha256(payload).hexdigest(), "test-scanner")
    first_stage = storage.stage(tenant, document, uuid4(), io.BytesIO(payload), max_bytes=1024)
    first = storage.promote_derived(first_stage, scan, edit_set_id=edit_set)
    second_stage = storage.stage(tenant, document, uuid4(), io.BytesIO(payload), max_bytes=1024)
    second = storage.promote_derived(second_stage, scan, edit_set_id=edit_set)

    assert first.key == second.key
    assert first.key.startswith(f"{tenant}/derived/{document}/{edit_set}/")
    assert first.path.read_bytes() == payload
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o400
    assert not first_stage.path.exists() and not second_stage.path.exists()


def test_derived_storage_requires_clean_digest_matched_scan(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    tenant, document = uuid4(), uuid4()
    staged = storage.stage(tenant, document, uuid4(), io.BytesIO(b"candidate"), max_bytes=1024)
    with pytest.raises(StorageRejected, match="clean_scan_required"):
        storage.promote_derived(staged, ScanResult("error", staged.sha256, "test"), edit_set_id=uuid4())
    with pytest.raises(StorageRejected, match="scan_digest_mismatch"):
        storage.promote_derived(staged, ScanResult("clean", "0" * 64, "test"), edit_set_id=uuid4())
    assert staged.path.exists()
