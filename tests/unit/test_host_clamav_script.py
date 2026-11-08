from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_clamav_host_validator_imports_project_when_run_outside_repository(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/host/validate_clamav.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "configured ClamAV Unix socket" in result.stdout
