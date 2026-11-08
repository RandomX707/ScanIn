from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import UUID

from packages.ingestion.detect import DetectionResult, detect_staged
from packages.ingestion.models import ScanResult
from packages.storage.local import ImmutableLocalStorage, QuarantinedUpload, StorageRejected, StoredFile


class Scanner(Protocol):
    def scan(self, path: str | Path) -> ScanResult: ...


@dataclass(frozen=True)
class IngestionResult:
    outcome: str
    detection: DetectionResult
    scan: ScanResult | None
    stored_file: StoredFile | None
    quarantine: QuarantinedUpload | None
    staged_path: Path | None
    normal_viewer_available: bool


class IngestionOrchestrator:
    """Stages, shallow-detects, scans, and promotes only a clean source.

    This orchestrator does not parse ZIP/XML contents or expose staged objects.
    Restricted parsing is a separate worker boundary added in the worker task.
    """

    def __init__(self, storage: ImmutableLocalStorage, scanner: Scanner, *, max_bytes: int = 50 * 1024 * 1024):
        if not 1 <= max_bytes <= 50 * 1024 * 1024:
            raise ValueError("max_bytes_out_of_range")
        self.storage = storage
        self.scanner = scanner
        self.max_bytes = max_bytes

    def process(self, tenant_id: UUID, document_id: UUID, upload_id: UUID,
                stream: BinaryIO, *, filename: str) -> IngestionResult:
        staged = self.storage.stage(tenant_id, document_id, upload_id, stream, max_bytes=self.max_bytes)
        detection = detect_staged(staged.path, size=staged.size_bytes, sha256=staged.sha256, max_bytes=self.max_bytes)
        if detection.capability.value == "Rejected":
            quarantine = self.storage.quarantine(staged)
            return IngestionResult("rejected", detection, None, None, quarantine, None, False)
        try:
            scan = self.scanner.scan(staged.path)
        except Exception:
            # Do not log exception details; a third-party message may expose paths or content.
            scan = ScanResult("error", staged.sha256, "scanner", reason_code="scanner_failure")
        if scan.status == "infected":
            quarantine = self.storage.quarantine(staged)
            return IngestionResult("quarantined", detection, scan, None, quarantine, None, False)
        if scan.status != "clean":
            return IngestionResult("scan_not_verified", detection, scan, None, None, staged.path, False)
        stored = self.storage.promote_original(staged, scan)
        return IngestionResult("stored_private", detection, scan, stored, None, None, False)
