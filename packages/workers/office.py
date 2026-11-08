from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


class OfficeRenderError(RuntimeError):
    """LibreOffice render could not be completed inside the no-network sandbox."""


def render_docx_pdf(source: bytes, *, timeout_seconds: int = 45) -> bytes:
    """Render a DOCX in an isolated LibreOffice profile and macOS deny-by-default sandbox."""
    soffice = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    sandbox_exec = Path("/usr/bin/sandbox-exec")
    if not soffice.is_file() or not sandbox_exec.is_file():
        raise OfficeRenderError("office_renderer_unavailable")
    with tempfile.TemporaryDirectory(prefix="channel360-lo-") as temp_name:
        base = Path(temp_name)
        input_dir, output_dir, profile, home = (base / name for name in ("input", "output", "profile", "home"))
        for path in (input_dir, output_dir, profile, home):
            path.mkdir(mode=0o700)
        docx = input_dir / "document.docx"
        docx.write_bytes(source)
        (profile / "user").mkdir(mode=0o700)
        # LibreOffice's per-job profile forces its macro security setting to Very High.
        registry = profile / "user" / "registrymodifications.xcu"
        registry.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry" '
            'xmlns:xs="http://www.w3.org/2001/XMLSchema">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '</item></oor:items>', encoding="utf-8")
        sandbox_profile = base / "office.sb"
        read_paths = [str(soffice.parent.parent), "/System/Library", "/usr/lib", "/usr/share/zoneinfo",
                      "/Library/Fonts", "/System/Library/Fonts", str(base)]
        metadata_paths = [str(soffice), *(read_paths), str(base)]
        rules = [
            '(version 1)', '(import "dyld-support.sb")',
            '(deny default)', '(allow process-fork)', '(allow sysctl-read)', '(deny network*)',
            '(allow process-exec (subpath "/Applications/LibreOffice.app"))',
            '(allow file-read-metadata file-test-existence '
            + " ".join(f'(path-ancestors "{path}")' for path in metadata_paths) + ")",
            '(allow file-read* ' + " ".join(f'(subpath "{path}")' for path in read_paths) + ")",
            f'(allow file-write* (subpath "{base}"))',
        ]
        sandbox_profile.write_text("\n".join(rules), encoding="utf-8")
        profile_url = profile.as_uri()
        command = [str(sandbox_exec), "-f", str(sandbox_profile), str(soffice), "--headless", "--nologo",
                   "--nodefault", "--nolockcheck", "--norestore", "--nofirststartwizard",
                   f"-env:UserInstallation={profile_url}", "--convert-to", "pdf", "--outdir", str(output_dir), str(docx)]
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
               "SAL_USE_VCLPLUGIN": "svp", "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout_seconds, env=env, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OfficeRenderError("office_render_failed") from exc
        rendered = output_dir / "document.pdf"
        if result.returncode != 0 or not rendered.is_file() or rendered.stat().st_size == 0:
            raise OfficeRenderError("office_render_failed")
        return rendered.read_bytes()
