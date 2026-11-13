"""Sandboxed, preview-only Office -> PDF rendering (LibreOffice, local, no network).

Preview only: the produced PDF is never an editing source of truth and is never saved as a document version.
Isolation (fails closed, never falls back to an unconfined run):
  * the document is COPIED into a private scratch directory; the converter never sees storage paths;
  * macOS Seatbelt profile: IP network denied, file writes limited to the scratch directory (+ system temp),
    file reads under /Users denied except the scratch directory;
  * a fresh LibreOffice profile with macros disabled and link/external-content updates set to "never";
  * wall-clock timeout, process-group kill, output size and page-count caps.
Confinement comes from packages/workers/sandbox.py: Seatbelt on macOS (self-tested here), Bubblewrap on Linux (implemented, NOT VERIFIED in
the development environment). The renderer is available only where the sandbox passed its runtime self-test; otherwise it reports unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from packages.workers.sandbox import SandboxFailed, SandboxPolicy, SandboxUnavailable, confined_runner, resolve_tool, sandbox_status

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


# Windows-only fonts Word files often name for LATIN text. When they are not installed the renderer would pick an arbitrary fallback
# (for example a Hebrew serif face); a conventional Latin serif/sans is a far better stand-in. Override with IDW_OFFICE_FONT_MAP (JSON).
DEFAULT_FONT_MAP = {
    "MingLiU_HKSCS-ExtB": "Times New Roman", "MingLiU_HKSCS": "Times New Roman", "MingLiU": "Times New Roman", "PMingLiU": "Times New Roman", "PMingLiU-ExtB": "Times New Roman",
    "SimSun": "Times New Roman", "SimSun-ExtB": "Times New Roman", "NSimSun": "Times New Roman", "MS Mincho": "Times New Roman", "MS PMincho": "Times New Roman",
    "Microsoft YaHei": "Arial", "SimHei": "Arial", "MS Gothic": "Arial", "MS PGothic": "Arial", "Microsoft JhengHei": "Arial", "Malgun Gothic": "Arial",
}


def font_map() -> dict[str, str]:
    mapping = dict(DEFAULT_FONT_MAP)
    configured = os.environ.get("IDW_OFFICE_FONT_MAP")
    if configured:
        try:
            extra = json.loads(configured)
            mapping.update({str(k): str(v) for k, v in extra.items() if isinstance(k, str) and isinstance(v, str)})
        except ValueError:
            pass
    return mapping


def _font_substitution_xml(mapping: dict[str, str]) -> str:
    from xml.sax.saxutils import escape
    nodes = "".join(
        f'<node oor:name="{i}" oor:op="replace"><prop oor:name="Always" oor:op="fuse"><value>true</value></prop><prop oor:name="OnScreenOnly" oor:op="fuse"><value>false</value></prop>'
        f'<prop oor:name="ReplaceFont" oor:op="fuse"><value>{escape(k)}</value></prop><prop oor:name="SubstituteFont" oor:op="fuse"><value>{escape(v)}</value></prop></node>'
        for i, (k, v) in enumerate(mapping.items()))
    return ('<item oor:path="/org.openoffice.Office.Common/Font/Substitution"><prop oor:name="Replacement" oor:op="fuse"><value>true</value></prop></item>'
            f'<item oor:path="/org.openoffice.Office.Common/Font/Substitution"><node oor:name="FontPairs">{nodes}</node></item>')


class OfficeRenderUnavailable(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class OfficeRenderRefused(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def seatbelt_profile(scratch: Path) -> str:
    """Kept for tests/documentation: the macOS profile used for the Office renderer."""
    from packages.workers.sandbox import SandboxPolicy as _P, SeatbeltRunner
    return SeatbeltRunner.profile(_P(scratch=scratch))


class SandboxedOfficeRenderer:
    """Preview-only LibreOffice rendering through the platform sandbox abstraction (see packages/workers/sandbox.py)."""

    def __init__(self, *, timeout_seconds: int = 120, runner=None):
        self.timeout_seconds = timeout_seconds
        self._runner = runner
        self.tool = resolve_tool("soffice")

    def reason_unavailable(self) -> str | None:
        if self.tool is None:
            return "office_renderer_missing"
        if self._runner is None:
            status = sandbox_status()
            if not status.verified_here:
                return "office_renderer_sandbox_unavailable"
        return None

    def available(self) -> bool:
        return self.reason_unavailable() is None

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
        try:
            runner = self._runner or confined_runner()
        except SandboxUnavailable:
            raise OfficeRenderUnavailable("office_renderer_sandbox_unavailable") from None
        scratch = Path(tempfile.mkdtemp(prefix="channel360-office-render-", dir="/private/tmp" if sys.platform == "darwin" else None)).resolve()
        os.chmod(scratch, 0o700)
        try:
            for name in ("in", "out", "home", "tmp"):
                (scratch / name).mkdir(mode=0o700)
            profile_user = scratch / "profile" / "user"
            profile_user.mkdir(parents=True, mode=0o700)
            (profile_user / "registrymodifications.xcu").write_text(_REGISTRY.replace("</oor:items>", _font_substitution_xml(font_map()) + "</oor:items>"), encoding="utf-8")
            source = scratch / "in" / f"input{suffix}"
            source.write_bytes(data)
            os.chmod(source, 0o400)
            args = ["--headless", "--norestore", "--nolockcheck", "--nologo", "--nodefault", "--invisible", f"-env:UserInstallation=file://{scratch / 'profile'}",
                    "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(scratch / "out"), str(source)]
            policy = SandboxPolicy(scratch=scratch, timeout_seconds=self.timeout_seconds, cpu_seconds=self.timeout_seconds * 2, max_file_bytes=MAX_OUTPUT_BYTES + 1024 * 1024,
                                   env=(("HOME", str(scratch / "home")), ("TMPDIR", str(scratch / "tmp"))))
            try:
                result = runner.run(self.tool, args, policy)
            except SandboxFailed as failed:
                raise OfficeRenderUnavailable("office_render_timeout" if failed.reason_code == "sandbox_timeout" else failed.reason_code) from None
            except SandboxUnavailable:
                raise OfficeRenderUnavailable("office_renderer_sandbox_unavailable") from None
            produced = scratch / "out" / "input.pdf"
            if result.returncode != 0 or not produced.is_file():
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
