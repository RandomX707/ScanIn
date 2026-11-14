"""Parent-side runner for the sandboxed PDF text-edit worker."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from packages.workers.isolation import RestrictedSubprocess, RestrictedWorkerFailed


class PdfEditUnavailable(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class PdfEditRefused(Exception):
    def __init__(self, code: str, message: str, edit_id: str | None = None):
        self.code, self.message, self.edit_id = code, message, edit_id
        super().__init__(code)


class RestrictedPdfEditor:
    """Runs the edit worker with a larger output allowance than the inspection parser (edited PDFs are files)."""

    def __init__(self, project_root: str | Path, runner: RestrictedSubprocess | None = None):
        self.project_root = Path(project_root).resolve()
        self.runner = runner or RestrictedSubprocess(self.project_root, max_output_bytes=96 * 1024 * 1024)

    def _run(self, job: dict[str, Any], source_path: Path, *, timeout_seconds: int) -> tuple[dict[str, Any], Path | None, Any]:
        tmp = tempfile.TemporaryDirectory(prefix="channel360-pdf-edit-")
        root = Path(tmp.name).resolve()
        scratch = root / "out"
        scratch.mkdir(mode=0o700)
        job = {**job, "source": str(source_path.resolve()), "out": str(scratch / "edited.pdf")}
        job_path = root / "job.json"
        job_path.write_text(json.dumps(job, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        os.chmod(job_path, 0o600)
        try:
            result = self.runner.run(
                [sys.executable, "-m", "packages.workers.pdf_edit_entry", "--job", str(job_path)],
                input_path=job_path, timeout_seconds=timeout_seconds,
                read_paths=(source_path.resolve(), root), write_paths=(scratch,),
                environment_overrides={"TMPDIR": str(scratch)})
        except (OSError, RestrictedWorkerFailed) as error:
            tmp.cleanup()
            raise PdfEditUnavailable(getattr(error, "reason_code", "pdf_edit_worker_unavailable")) from None
        try:
            data = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError):
            tmp.cleanup()
            raise PdfEditUnavailable("pdf_edit_response_invalid") from None
        return data, scratch / "edited.pdf", tmp

    def runs(self, source_path: Path, page: int, *, timeout_seconds: int = 60) -> dict[str, Any]:
        data, _, tmp = self._run({"mode": "runs", "page": page}, source_path, timeout_seconds=timeout_seconds)
        tmp.cleanup()
        if data.get("error"):
            raise PdfEditUnavailable(str(data["error"]))
        return data

    def outline(self, source_path: Path, *, timeout_seconds: int = 120) -> dict[str, Any]:
        data, _, tmp = self._run({"mode": "outline"}, source_path, timeout_seconds=timeout_seconds)
        tmp.cleanup()
        if data.get("error"):
            raise PdfEditUnavailable(str(data["error"]))
        return data

    def working_copy(self, source_path: Path, *, name: str, state: str, timeout_seconds: int = 180) -> tuple[bytes, dict[str, Any]]:
        data, out_path, tmp = self._run({"mode": "working_copy", "name": name, "state": state}, source_path, timeout_seconds=timeout_seconds)
        try:
            if data.get("error"):
                raise PdfEditRefused(str(data["error"]), str(data.get("message") or ""))
            return out_path.read_bytes(), data
        finally:
            tmp.cleanup()

    def edit(self, source_path: Path, edits: list[dict[str, str]], *, timeout_seconds: int = 120) -> tuple[bytes, dict[str, Any]]:
        data, out_path, tmp = self._run({"mode": "edit", "edits": edits}, source_path, timeout_seconds=timeout_seconds)
        try:
            if data.get("error"):
                if data["error"] in {"pdf_edit_worker_failed", "edit_count_invalid", "duplicate_target", "page_out_of_range"}:
                    raise PdfEditUnavailable(str(data["error"]))
                raise PdfEditRefused(str(data["error"]), str(data.get("message") or ""), data.get("edit_id"))
            return out_path.read_bytes(), data
        finally:
            tmp.cleanup()
