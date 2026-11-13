"""Sandboxed, preview-only Office -> PDF rendering (LibreOffice, local, no network).

Preview only: the produced PDF is never an editing source of truth and is never saved as a document version.
Isolation (fails closed, never falls back to an unconfined run):
  * the document is COPIED into a private scratch directory; the converter never sees storage paths;
  * macOS Seatbelt profile: IP network denied, file writes limited to the scratch directory (+ system temp),
    file reads under /Users denied except the scratch directory;
  * a fresh LibreOffice profile with macros disabled and link/external-content updates set to "never";
  * wall-clock timeout, process-group kill, output size and page-count caps.
Linux (bubblewrap) is not implemented for this worker: it reports unavailable rather than running unconfined.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_OUTPUT_BYTES = 96 * 1024 * 1024
MAX_PAGES = 400
MAX_INPUT_BYTES = 64 * 1024 * 1024
SUPPORTED = {".docx": "docx", ".doc": "doc", ".odt": "odt", ".rtf": "rtf", ".pptx": "pptx"}

_REGISTRY = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="FirstRun" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Field" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
</oor:items>
"""


def _magic_ok(data: bytes, suffix: str) -> bool:
    if suffix in {".docx", ".pptx", ".odt"}:
        return data[:4] == b"PK\x03\x04"
    if suffix == ".doc":
        return data[:8] == bytes.fromhex("d0cf11e0a1b11ae1")
    if suffix == ".rtf":
        return data[:5] == b"{\\rtf"
    return False


class OfficeRenderUnavailable(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class OfficeRenderRefused(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _find_soffice() -> str | None:
    configured = os.environ.get("IDW_SOFFICE_PATH")
    candidates = [configured] if configured else []
    candidates += ["/Applications/LibreOffice.app/Contents/MacOS/soffice", shutil.which("soffice") or ""]
    for item in candidates:
        if item and Path(item).is_file():
            return str(Path(item).resolve())
    return None


def seatbelt_profile(scratch: Path) -> str:
    s = scratch.resolve().as_posix().replace('"', '\\"')
    return "\n".join((
        "(version 1)", "(allow default)",
        "(deny network-outbound (remote ip))", "(deny network-inbound (local ip))", "(deny network-bind (local ip))",
        "(deny file-write*)",
        f'(allow file-write* (subpath "{s}") (subpath "/dev") (subpath "/private/var/folders") (subpath "/private/var/tmp"))',
        '(deny file-read* (subpath "/Users"))',
        f'(allow file-read* (subpath "{s}"))',
    ))


class SandboxedOfficeRenderer:
    def __init__(self, *, timeout_seconds: int = 120, platform_name: str | None = None):
        self.timeout_seconds = timeout_seconds
        self.platform_name = platform_name or sys.platform
        self.soffice = _find_soffice()

    def available(self) -> bool:
        return self.soffice is not None and self.platform_name == "darwin" and Path("/usr/bin/sandbox-exec").is_file()

    def reason_unavailable(self) -> str | None:
        if self.soffice is None:
            return "office_renderer_missing"
        if self.platform_name != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            return "office_renderer_sandbox_unavailable"
        return None

    def render_pdf(self, data: bytes, suffix: str) -> bytes:
        suffix = suffix.lower()
        if suffix not in SUPPORTED:
            raise OfficeRenderRefused("office_render_type_unsupported")
        if len(data) > MAX_INPUT_BYTES:
            raise OfficeRenderRefused("office_render_input_too_large")
        if not _magic_ok(data, suffix):
            raise OfficeRenderRefused("office_render_content_mismatch")
        reason = self.reason_unavailable()
        if reason:
            raise OfficeRenderUnavailable(reason)
        if os.geteuid() == 0:
            raise OfficeRenderUnavailable("office_renderer_root_forbidden")
        scratch = Path(tempfile.mkdtemp(prefix="channel360-office-render-", dir="/private/tmp")).resolve()
        os.chmod(scratch, 0o700)
        try:
            (scratch / "in").mkdir(mode=0o700)
            (scratch / "out").mkdir(mode=0o700)
            (scratch / "home").mkdir(mode=0o700)
            (scratch / "tmp").mkdir(mode=0o700)
            profile_user = scratch / "profile" / "user"
            profile_user.mkdir(parents=True, mode=0o700)
            (profile_user / "registrymodifications.xcu").write_text(_REGISTRY, encoding="utf-8")
            source = scratch / "in" / f"input{suffix}"
            source.write_bytes(data)
            os.chmod(source, 0o400)
            sb = seatbelt_profile(scratch)
            command = ["/usr/bin/sandbox-exec", "-p", sb, self.soffice, "--headless", "--norestore", "--nolockcheck",
                       "--nologo", "--nodefault", "--invisible", f"-env:UserInstallation=file://{scratch / 'profile'}",
                       "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(scratch / "out"), str(source)]
            env = {"HOME": str(scratch / "home"), "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "TMPDIR": str(scratch / "tmp")}
            process = subprocess.Popen(command, cwd=str(scratch / "out"), env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                self._kill(process)
                raise OfficeRenderUnavailable("office_render_timeout") from None
            finally:
                self._kill(process)
            produced = scratch / "out" / "input.pdf"
            if process.returncode != 0 or not produced.is_file():
                raise OfficeRenderRefused("office_render_failed")
            size = produced.stat().st_size
            if size > MAX_OUTPUT_BYTES or size < 100:
                raise OfficeRenderRefused("office_render_output_invalid")
            out = produced.read_bytes()
            if not out.startswith(b"%PDF-"):
                raise OfficeRenderRefused("office_render_output_invalid")
            from pypdf import PdfReader
            import io
            if len(PdfReader(io.BytesIO(out)).pages) > MAX_PAGES:
                raise OfficeRenderRefused("office_render_too_many_pages")
            return out
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
