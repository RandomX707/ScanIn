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
    """Render a DOCX to PDF for layout verification.

    This used to carry its own Seatbelt profile, which could not start LibreOffice on current macOS (the profile lacked the grants the
    verified confinement now carries), so every visual verification was BLOCKED. There is now ONE confinement implementation:
    `SandboxedOfficeRenderer` (deny-default Seatbelt / Bubblewrap, runtime self-test, fail closed). Any refusal or unavailability stays
    an `OfficeRenderError`, so callers keep failing closed.
    """
    from packages.workers.office_render import OfficeRenderRefused, OfficeRenderUnavailable, SandboxedOfficeRenderer
    try:
        return SandboxedOfficeRenderer(timeout_seconds=timeout_seconds).render_pdf(source, ".docx")
    except (OfficeRenderRefused, OfficeRenderUnavailable) as failure:
        raise OfficeRenderError(getattr(failure, "reason_code", "office_render_failed")) from failure
