from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Capability(StrEnum):
    EDITABLE = "Editable"
    READ_ONLY = "Read-only"
    LIMITED = "Limited"
    REJECTED = "Rejected"


@dataclass(frozen=True)
class DetectionResult:
    media_type: str
    size_bytes: int
    sha256: str
    capability: Capability
    reasons: tuple[str, ...]


def detect_bytes(content: bytes, *, filename: str = "", max_bytes: int = 50 * 1024 * 1024) -> DetectionResult:
    """Classify by shallow magic bytes only; never parse untrusted container data here."""
    digest = hashlib.sha256(content).hexdigest()
    return detect_prefix(content[:4096], size=len(content), sha256=digest, max_bytes=max_bytes)


def detect_staged(path: str | Path, *, size: int, sha256: str,
                  max_bytes: int = 50 * 1024 * 1024) -> DetectionResult:
    """Detect from a bounded prefix of a privately staged file without loading it all."""
    with Path(path).open("rb") as source:
        prefix = source.read(4096)
    return detect_prefix(prefix, size=size, sha256=sha256, max_bytes=max_bytes)


def detect_prefix(prefix: bytes, *, size: int, sha256: str,
                  max_bytes: int = 50 * 1024 * 1024) -> DetectionResult:
    if size < 0 or len(sha256) != 64:
        raise ValueError("invalid_file_metadata")
    if size == 0:
        return DetectionResult("application/octet-stream", size, sha256, Capability.REJECTED, ("empty_file",))
    if size > max_bytes:
        return DetectionResult("application/octet-stream", size, sha256, Capability.REJECTED, ("file_too_large",))

    if _is_executable(prefix[:16]):
        return DetectionResult("application/x-executable", size, sha256, Capability.REJECTED, ("executable_content",))
    if prefix.startswith(b"%PDF-"):
        return DetectionResult("application/pdf", size, sha256, Capability.READ_ONLY, ("adapter_not_implemented",))
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return DetectionResult("image/png", size, sha256, Capability.READ_ONLY, ("adapter_not_implemented",))
    if prefix.startswith(b"\xff\xd8\xff"):
        return DetectionResult("image/jpeg", size, sha256, Capability.READ_ONLY, ("adapter_not_implemented",))
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return DetectionResult("image/gif", size, sha256, Capability.READ_ONLY, ("adapter_not_implemented",))
    if prefix.startswith(b"PK\x03\x04"):
        # ZIP central directory and XML parsing are deferred to the restricted parser worker.
        return DetectionResult("application/zip", size, sha256, Capability.LIMITED,
                               ("container_inspection_requires_sandbox",))
    if prefix.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        return DetectionResult("application/x-ole-storage", size, sha256, Capability.LIMITED,
                               ("legacy_or_encrypted_office_requires_sandbox",))

    stripped = prefix.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith((b"{", b"[")):
        return DetectionResult("application/json", size, sha256, Capability.READ_ONLY,
                               ("adapter_not_implemented",))
    if _looks_textual(prefix):
        return DetectionResult("text/plain", size, sha256, Capability.READ_ONLY, ("adapter_not_implemented",))
    return DetectionResult("application/octet-stream", size, sha256, Capability.LIMITED, ("unknown_file_type",))


def _is_executable(prefix: bytes) -> bool:
    return (
        prefix.startswith(b"MZ")
        or prefix.startswith(b"\x7fELF")
        or prefix[:4] in {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"}
        or prefix.startswith(b"#!")
    )


def _looks_textual(content: bytes) -> bool:
    sample = content[:4096]
    return b"\x00" not in sample and sum(byte < 9 or 13 < byte < 32 for byte in sample) < max(1, len(sample) // 20)
