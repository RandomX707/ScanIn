from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.workers.isolation import RestrictedSubprocess, RestrictedWorkerFailed


class ParserNotVerified(RuntimeError):
    """Inspection could not be completed inside the required sandbox."""


@dataclass(frozen=True)
class ParserReport:
    status: str
    sha256: str
    size_bytes: int
    media_type: str
    capability: dict[str, Any]
    adapter: str | None
    adapter_version: str | None
    sheets: tuple[str, ...]
    features: tuple[str, ...]
    findings: tuple[str, ...] = ()
    score: float = 0.0
    targets: tuple[dict[str, Any], ...] = ()
    targets_truncated: bool = False


class RestrictedParser:
    def __init__(self, project_root: str | Path, runner: RestrictedSubprocess | None = None):
        self.project_root = Path(project_root).resolve()
        self.runner = runner or RestrictedSubprocess(self.project_root)

    def inspect(self, source_path: str | Path, *, filename: str | None = None,
                include_targets: bool = False, timeout_seconds: int = 30) -> ParserReport:
        source = Path(source_path).resolve(strict=True)
        size = source.stat().st_size
        if not source.is_file() or size > 50 * 1024 * 1024:
            raise ParserNotVerified("parser_input_invalid")
        command = [sys.executable, "-m", "packages.workers.parse_entry", "--input", str(source),
                   "--filename", filename or source.name]
        if include_targets:
            command.append("--targets")
        try:
            result = self.runner.run(command, input_path=source, timeout_seconds=timeout_seconds)
        except (OSError, RestrictedWorkerFailed) as error:
            raise ParserNotVerified("restricted_parser_failed") from error
        if result.returncode != 0:
            raise ParserNotVerified("restricted_parser_failed")
        try:
            payload = json.loads(result.stdout)
            digest_state = hashlib.sha256()
            with source.open("rb") as file_handle:
                for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                    digest_state.update(chunk)
            digest = digest_state.hexdigest()
            report = ParserReport(
                status=payload["status"], sha256=payload["sha256"], size_bytes=payload["size_bytes"],
                media_type=payload["media_type"], capability=payload["capability"],
                adapter=payload.get("adapter"), adapter_version=payload.get("adapter_version"),
                sheets=tuple(payload.get("sheets", ())), features=tuple(payload.get("features", ())),
                findings=tuple(payload.get("findings", ())), score=float(payload.get("score", 0.0)),
                targets=tuple(payload.get("targets", ())), targets_truncated=bool(payload.get("targets_truncated", False)),
            )
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            raise ParserNotVerified("restricted_parser_response_invalid") from None
        if report.sha256 != digest or report.size_bytes != size:
            raise ParserNotVerified("restricted_parser_result_source_mismatch")
        return report


# Name reflects the current parser scope; later isolated adapters can share this boundary.
RestrictedXlsxInspector = RestrictedParser
