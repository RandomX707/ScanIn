from __future__ import annotations

import hashlib
import io
from uuid import uuid4

from packages.ingestion.models import ScanResult
from packages.ingestion.pipeline import IngestionOrchestrator
from packages.storage.local import ImmutableLocalStorage


class VerdictScanner:
    def __init__(self, status: str):
        self.status = status
    def scan(self, path):
        return ScanResult(self.status, hashlib.sha256(path.read_bytes()).hexdigest(), "unit-scanner",
                          reason_code="malware_detected" if self.status == "infected" else None)


def test_clean_unsupported_document_is_stored_but_not_viewable(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    orchestrator = IngestionOrchestrator(storage, VerdictScanner("clean"), max_bytes=1000)
    result = orchestrator.process(uuid4(), uuid4(), uuid4(), io.BytesIO(b"%PDF-1.7 fake"), filename="x.pdf")

    assert result.outcome == "stored_private"
    assert result.detection.media_type == "application/pdf"
    assert result.stored_file is not None
    assert result.normal_viewer_available is False


def test_infected_content_is_quarantined_and_never_promoted(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    orchestrator = IngestionOrchestrator(storage, VerdictScanner("infected"), max_bytes=1000)
    result = orchestrator.process(uuid4(), uuid4(), uuid4(), io.BytesIO(b"%PDF-1.7 eicar"), filename="x.pdf")

    assert result.outcome == "quarantined"
    assert result.stored_file is None
    assert result.quarantine is not None and result.quarantine.path.exists()
    assert not list((tmp_path / "objects").glob("**/originals/**"))


def test_scanner_error_fails_closed_and_keeps_file_staged(tmp_path):
    storage = ImmutableLocalStorage(tmp_path / "objects")
    orchestrator = IngestionOrchestrator(storage, VerdictScanner("error"), max_bytes=1000)
    result = orchestrator.process(uuid4(), uuid4(), uuid4(), io.BytesIO(b"%PDF-1.7"), filename="x.pdf")

    assert result.outcome == "scan_not_verified"
    assert result.stored_file is None
    assert result.staged_path is not None and result.staged_path.exists()
