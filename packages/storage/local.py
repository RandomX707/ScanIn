from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from packages.ingestion.models import ScanResult


class StorageRejected(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class StagedUpload:
    tenant_id: UUID
    document_id: UUID
    upload_id: UUID
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class StoredFile:
    tenant_id: UUID
    document_id: UUID
    path: Path
    key: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class QuarantinedUpload:
    tenant_id: UUID
    upload_id: UUID
    path: Path
    sha256: str
    size_bytes: int


class ImmutableLocalStorage:
    """Local development backend with private staging and write-once originals.

    The API must not serve this root as static content. Originals are moved out of
    staging only after a clean scan result whose digest matches the staged bytes.
    """

    def __init__(self, root: str | Path):
        requested_root = Path(root).expanduser().absolute()
        if requested_root.is_symlink():
            raise StorageRejected("symlink_path_rejected")
        requested_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if requested_root.is_symlink() or not requested_root.is_dir():
            raise StorageRejected("unsafe_storage_root")
        self.root = requested_root.resolve(strict=True)

    def stage(self, tenant_id: UUID, document_id: UUID, upload_id: UUID,
              stream: BinaryIO, *, max_bytes: int) -> StagedUpload:
        if max_bytes <= 0:
            raise ValueError("max_bytes_must_be_positive")
        staging_dir = self.root / ".staging" / str(tenant_id)
        self._ensure_private_directory(staging_dir)
        path = staging_dir / f"{upload_id}.part"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            raise StorageRejected("upload_id_already_staged") from None
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as destination:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise StorageRejected("upload_stream_not_binary")
                    size += len(chunk)
                    if size > max_bytes:
                        raise StorageRejected("upload_too_large")
                    digest.update(chunk)
                    destination.write(chunk)
                if size == 0:
                    raise StorageRejected("empty_upload")
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return StagedUpload(tenant_id, document_id, upload_id, path, size, digest.hexdigest())

    def promote_original(self, staged: StagedUpload, scan: ScanResult) -> StoredFile:
        self._validate_staged_file(staged)
        if scan.status != "clean":
            raise StorageRejected("clean_scan_required")
        if scan.sha256 != staged.sha256:
            raise StorageRejected("scan_digest_mismatch")
        actual_size, actual_digest = self._digest_file(staged.path)
        if actual_size != staged.size_bytes or actual_digest != staged.sha256:
            raise StorageRejected("staged_file_integrity_mismatch")

        original_dir = self.root / str(staged.tenant_id) / "originals" / str(staged.document_id)
        self._ensure_private_directory(original_dir)
        target = original_dir / staged.sha256
        created = False
        if target.is_symlink():
            raise StorageRejected("symlink_path_rejected")
        try:
            os.link(staged.path, target, follow_symlinks=False)
            created = True
        except FileExistsError:
            existing_size, existing_hash = self._digest_file(target)
            if existing_hash != staged.sha256 or existing_size != staged.size_bytes:
                raise StorageRejected("immutable_storage_collision") from None
        try:
            target.chmod(stat.S_IRUSR)
        except OSError:
            if created:
                target.unlink(missing_ok=True)
            raise StorageRejected("immutable_permissions_failed") from None
        staged.path.unlink()
        key = target.relative_to(self.root).as_posix()
        return StoredFile(staged.tenant_id, staged.document_id, target, key, staged.size_bytes, staged.sha256)

    def promote_derived(self, staged: StagedUpload, scan: ScanResult, *, edit_set_id: UUID) -> StoredFile:
        """Publish a verified, clean derivative at a content-addressed immutable key."""
        self._validate_staged_file(staged)
        if scan.status != "clean":
            raise StorageRejected("clean_scan_required")
        if scan.sha256 != staged.sha256:
            raise StorageRejected("scan_digest_mismatch")
        actual_size, actual_digest = self._digest_file(staged.path)
        if actual_size != staged.size_bytes or actual_digest != staged.sha256:
            raise StorageRejected("staged_file_integrity_mismatch")
        derived_dir = self.root / str(staged.tenant_id) / "derived" / str(staged.document_id) / str(edit_set_id)
        self._ensure_private_directory(derived_dir)
        target = derived_dir / staged.sha256
        created = False
        if target.is_symlink():
            raise StorageRejected("symlink_path_rejected")
        try:
            os.link(staged.path, target, follow_symlinks=False)
            created = True
        except FileExistsError:
            existing_size, existing_hash = self._digest_file(target)
            if existing_hash != staged.sha256 or existing_size != staged.size_bytes:
                raise StorageRejected("immutable_storage_collision") from None
        try:
            target.chmod(stat.S_IRUSR)
        except OSError:
            if created:
                target.unlink(missing_ok=True)
            raise StorageRejected("immutable_permissions_failed") from None
        staged.path.unlink()
        key = target.relative_to(self.root).as_posix()
        return StoredFile(staged.tenant_id, staged.document_id, target, key, staged.size_bytes, staged.sha256)

    def discard_staged(self, staged: StagedUpload) -> None:
        self._validate_staged_file(staged)
        staged.path.unlink()

    def quarantine(self, staged: StagedUpload) -> QuarantinedUpload:
        self._validate_staged_file(staged)
        quarantine_dir = self.root / ".quarantine" / str(staged.tenant_id)
        self._ensure_private_directory(quarantine_dir)
        target = quarantine_dir / f"{staged.upload_id}-{staged.sha256}"
        if target.exists() or target.is_symlink():
            raise StorageRejected("quarantine_target_exists")
        os.replace(staged.path, target)
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return QuarantinedUpload(staged.tenant_id, staged.upload_id, target, staged.sha256, staged.size_bytes)

    def read_original(self, stored: StoredFile) -> bytes:
        expected = self.root / stored.key
        if expected != stored.path or not self._is_within_root(expected):
            raise StorageRejected("storage_key_outside_root")
        if expected.is_symlink() or not expected.is_file():
            raise StorageRejected("stored_file_unavailable")
        size, digest = self._digest_file(expected)
        if size != stored.size_bytes or digest != stored.sha256:
            raise StorageRejected("stored_file_integrity_mismatch")
        return expected.read_bytes()

    def _validate_staged_file(self, staged: StagedUpload) -> None:
        expected_dir = self.root / ".staging" / str(staged.tenant_id)
        expected_path = expected_dir / f"{staged.upload_id}.part"
        if staged.path != expected_path or not self._is_within_root(expected_path):
            raise StorageRejected("staged_path_outside_root")
        if expected_dir.is_symlink() or expected_path.is_symlink() or not expected_path.is_file():
            raise StorageRejected("staged_file_unavailable")

    def _is_within_root(self, path: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(self.root)
            return True
        except ValueError:
            return False

    def _ensure_private_directory(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            raise StorageRejected("storage_path_outside_root") from None
        current = self.root
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise StorageRejected("symlink_path_rejected")
            current.mkdir(mode=0o700, exist_ok=True)
            if current.is_symlink() or not current.is_dir():
                raise StorageRejected("unsafe_storage_directory")
            current.chmod(0o700)

    @staticmethod
    def _digest_file(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise StorageRejected("stored_path_not_regular_file")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()
