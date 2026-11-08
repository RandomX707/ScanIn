from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ScanStatus = Literal["clean", "infected", "error", "not_scanned"]


@dataclass(frozen=True)
class ScanResult:
    """Content-bound scanner result. Only `clean` results can leave private staging."""

    status: ScanStatus
    sha256: str
    scanner: str
    signature_version: str | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"clean", "infected", "error", "not_scanned"}:
            raise ValueError("invalid_scan_status")
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256):
            raise ValueError("invalid_scan_sha256")
        if not self.scanner:
            raise ValueError("scanner_name_required")
