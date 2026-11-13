"""Parent-side runner for the sandboxed XLSX index worker (cancellable, bounded)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from packages.workers.isolation import RestrictedSubprocess, RestrictedWorkerFailed


def _sqlite_library_dirs() -> tuple[Path, ...]:
    """Directories of the shared SQLite library the interpreter links against (macOS Homebrew keeps it outside the Python prefix).
    Read-only access to exactly these directories is added to the confinement; nothing else is opened up."""
    if sys.platform != "darwin":
        return ()
    try:
        import _sqlite3
        out = subprocess.run(["/usr/bin/otool", "-L", _sqlite3.__file__], capture_output=True, text=True, timeout=10).stdout
    except (ImportError, OSError, subprocess.SubprocessError):
        return ()
    dirs: list[Path] = []
    for line in out.splitlines():
        match = re.match(r"\s+(/\S*sqlite\S*\.dylib)", line)
        if match and not match.group(1).startswith(("/usr/lib/", "/System/")):
            real = Path(match.group(1))
            for candidate in (real.parent, real.resolve().parent):
                if candidate not in dirs and candidate.is_dir():
                    dirs.append(candidate)
    return tuple(dirs)


class XlsxIndexUnavailable(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class RestrictedXlsxIndexer:
    def __init__(self, project_root: str | Path, runner: RestrictedSubprocess | None = None):
        self.project_root = Path(project_root).resolve()
        self.runner = runner or RestrictedSubprocess(self.project_root, max_output_bytes=1024 * 1024 * 1024)   # also the per-file write ceiling: the index database may be large; stdout stays tiny

    def build(self, source_path: Path, final_dir: Path, *, cancel: threading.Event | None = None, timeout_seconds: int = 240) -> dict[str, Any]:
        """Build the index in a private scratch directory, then publish it atomically at final_dir (never half-written)."""
        tmp = tempfile.TemporaryDirectory(prefix="channel360-xlsx-index-")
        root = Path(tmp.name).resolve()
        scratch = root / "out"
        scratch.mkdir(mode=0o700)
        job = root / "job.json"
        job.write_text(json.dumps({"source": str(source_path.resolve()), "out": str(scratch)}), encoding="utf-8")
        os.chmod(job, 0o600)
        try:
            result = self.runner.run([sys.executable, "-m", "packages.workers.xlsx_index_entry", "--job", str(job)], input_path=job,
                                     timeout_seconds=timeout_seconds, read_paths=(source_path.resolve(), root, *_sqlite_library_dirs()), write_paths=(scratch,),
                                     environment_overrides={"TMPDIR": str(scratch)}, cancel=cancel)
            try:
                status = json.loads(result.stdout)
            except (ValueError, UnicodeDecodeError):
                raise XlsxIndexUnavailable("xlsx_index_response_invalid") from None
            if status.get("error"):
                raise XlsxIndexUnavailable(str(status["error"]))
            if not (scratch / "meta.json").is_file():
                raise XlsxIndexUnavailable("xlsx_index_missing")
            final_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            staging = final_dir.parent / f".{final_dir.name}.{os.getpid()}.building"
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(scratch, staging)
            if final_dir.exists():
                shutil.rmtree(staging, ignore_errors=True)
            else:
                os.replace(staging, final_dir)
            return status
        except RestrictedWorkerFailed as failed:
            raise XlsxIndexUnavailable(failed.reason_code) from None
        finally:
            tmp.cleanup()
