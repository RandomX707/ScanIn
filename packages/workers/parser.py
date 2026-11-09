from __future__ import annotations

import hashlib
import json
import base64
import os
import shutil
import sys
import tempfile
from decimal import Decimal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.workers.isolation import RestrictedSubprocess, RestrictedWorkerFailed
from packages.core.models.contracts import ChangePermission, CheckResult, Patch, PatchPlan, TypedValue, VerifyReport


class ParserNotVerified(RuntimeError):
    """Inspection could not be completed inside the required sandbox."""


class PdfVerificationWorkerUnavailable(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class RestrictedPdfVerifier:
    def __init__(self, parser: "RestrictedParser"):
        self.parser = parser

    def verify(self, source: bytes, candidate: bytes, plan: PatchPlan) -> VerifyReport:
        return self.parser.verify_pdf(source, candidate, plan)


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
    targets_page: dict[str, Any] | None = None
    targets_editable_total: int | None = None
    details: dict[str, Any] | None = None
    structural_summary: dict[str, Any] | None = None
    summary_evidence: tuple[dict[str, str], ...] = ()


class RestrictedParser:
    def __init__(self, project_root: str | Path, runner: RestrictedSubprocess | None = None):
        self.project_root = Path(project_root).resolve()
        self.runner = runner or RestrictedSubprocess(self.project_root)

    def inspect(self, source_path: str | Path, *, filename: str | None = None,
                include_targets: bool = False, include_summary_evidence: bool = False,
                timeout_seconds: int = 30, target_offset: int = 0,
                target_ids: list[str] | tuple[str, ...] | None = None) -> ParserReport:
        source = Path(source_path).resolve(strict=True)
        size = source.stat().st_size
        if not source.is_file() or size > 50 * 1024 * 1024:
            raise ParserNotVerified("parser_input_invalid")
        command = [sys.executable, "-m", "packages.workers.parse_entry", "--input", str(source),
                   "--filename", filename or source.name]
        if include_targets:
            command.append("--targets")
        if include_summary_evidence:
            command.append("--summary-evidence")
        if isinstance(target_offset, bool) or not isinstance(target_offset, int) or target_offset < 0:
            raise ParserNotVerified("parser_target_page_invalid")
        if target_offset:
            command.extend(["--target-offset", str(target_offset)])
        if target_ids is not None:
            command.extend(["--target-ids", json.dumps(list(target_ids), separators=(",", ":"))])
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
                targets_page=payload.get("targets_page"),
                targets_editable_total=payload.get("targets_editable_total"),
                details=payload.get("details"),
                structural_summary=payload.get("structural_summary"),
                summary_evidence=tuple(payload.get("summary_evidence", ())),
            )
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            raise ParserNotVerified("restricted_parser_response_invalid") from None
        if report.sha256 != digest or report.size_bytes != size:
            raise ParserNotVerified("restricted_parser_result_source_mismatch")
        return report

    def verify_pdf(self, source: bytes, candidate: bytes, plan: PatchPlan,
                   *, timeout_seconds: int = 90) -> VerifyReport:
        if not source or not candidate or len(source) > 50 * 1024 * 1024 or len(candidate) > 50 * 1024 * 1024:
            raise PdfVerificationWorkerUnavailable("pdf_verifier_input_invalid")
        payload = {
            "source": base64.b64encode(source).decode("ascii"),
            "candidate": base64.b64encode(candidate).decode("ascii"),
            "plan": _serialize_plan(plan),
        }
        qpdf = shutil.which("qpdf")
        if not qpdf:
            raise PdfVerificationWorkerUnavailable("qpdf_unavailable")
        qpdf_path = Path(qpdf)
        qpdf_real = qpdf_path.resolve()
        qpdf_prefix = qpdf_real.parent.parent
        with tempfile.TemporaryDirectory(prefix="channel360-pdf-worker-") as temporary:
            root = Path(temporary)
            path, scratch = root / "job.json", root / "tmp"
            scratch.mkdir(mode=0o700)
            path.write_text(json.dumps(payload, separators=(",", ":"), allow_nan=False), encoding="utf-8")
            os.chmod(path, 0o600)
            qpdf_dependencies = tuple(
                dependency for dependency in (
                    qpdf_path.parent, qpdf_prefix,
                    Path("/opt/homebrew/opt/jpeg-turbo"),
                    Path("/opt/homebrew/opt/openssl@4"),
                    Path("/opt/homebrew/opt/qpdf"),
                ) if dependency.exists()
            )
            try:
                result = self.runner.run(
                    [sys.executable, "-m", "packages.workers.pdf_verify_entry", "--input", str(path.resolve())],
                    input_path=path, timeout_seconds=timeout_seconds,
                    read_paths=(*qpdf_dependencies, scratch), write_paths=(scratch,),
                    executable_paths=(qpdf_path, qpdf_real),
                    environment_overrides={"PATH": f"{qpdf_path.parent}:/usr/bin:/bin",
                                           "TMPDIR": str(scratch.resolve())},
                )
            except (OSError, RestrictedWorkerFailed) as error:
                raise PdfVerificationWorkerUnavailable(getattr(error, "reason_code", "pdf_verifier_worker_unavailable")) from None
            return self._decode_pdf_verification_result(result)

    @staticmethod
    def _decode_pdf_verification_result(result) -> VerifyReport:
        if result.returncode != 0 and not result.stdout:
            raise PdfVerificationWorkerUnavailable("pdf_verifier_worker_failed")
        try:
            data = json.loads(result.stdout)
            if data.get("error"):
                suffix = "_".join(str(item) for item in data.get("stage", [])[-2:])
                raise PdfVerificationWorkerUnavailable(f"{data['error']}:{suffix}" if suffix else str(data["error"]))
            if result.returncode != 0:
                raise PdfVerificationWorkerUnavailable("pdf_verifier_worker_failed")
            checks = tuple(CheckResult(item["check"], item["status"], bool(item["critical"]), item["detail"])
                           for item in data["checks"])
            return VerifyReport(checks, data["outcome"], data["source_sha256"], data["output_sha256"])
        except PdfVerificationWorkerUnavailable:
            raise
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            raise PdfVerificationWorkerUnavailable("pdf_verifier_response_invalid") from None


def _value(value: dict[str, Any]) -> TypedValue:
    raw = value["value"]
    if value["type"] == "decimal":
        raw = Decimal(str(raw))
    return TypedValue(value["type"], raw, value.get("native"), value.get("display"))


def _serialize_plan(plan: PatchPlan) -> dict[str, Any]:
    return {
        "source_version_id": plan.source_version_id,
        "source_sha256": plan.source_sha256,
        "patches": [{"source_version_id": item.source_version_id, "target_id": item.target_id,
                     "expected_old_value": item.expected_old_value.canonical(),
                     "expected_fingerprint": item.expected_fingerprint,
                     "new_value": item.new_value.canonical()} for item in plan.patches],
        "permitted_changes": [{"part": item.part, "locator": item.locator, "operation": item.operation,
                               "old_fingerprint": item.old_fingerprint,
                               "expected_new_fingerprint": item.expected_new_fingerprint}
                              for item in plan.permitted_changes],
        "provenance": list(plan.provenance), "dependents": list(plan.dependents),
        "dependencies_unknown": plan.dependencies_unknown, "no_change": plan.no_change,
        "plan_sha256": plan.plan_sha256,
    }


# Name reflects the current parser scope; later isolated adapters can share this boundary.
RestrictedXlsxInspector = RestrictedParser
