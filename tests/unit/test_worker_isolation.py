from __future__ import annotations

import sys
import site
from pathlib import Path

import pytest

from packages.workers.isolation import IsolationUnavailable, RestrictedSubprocess, RestrictedWorkerFailed


def test_mac_parser_profile_denies_all_network_and_uses_default_deny():
    profile = RestrictedSubprocess.mac_profile(project_root="/repo", input_path="/private/source.xlsx")
    assert '(import "dyld-support.sb")' in profile
    assert '(literal "/repo")' in profile
    assert "(deny default)" in profile
    assert "(deny network*)" in profile
    assert "(allow network" not in profile
    assert "/private/source.xlsx" in profile


def test_parser_command_uses_network_restricted_launcher():
    runner = RestrictedSubprocess(project_root=".")
    plan = runner.build_command(["/python", "-c", "pass"], input_path="/private/source.xlsx")
    assert plan.command[0] == "/usr/bin/sandbox-exec"
    assert "-p" in plan.command
    assert "(deny network*)" in plan.profile
    assert plan.input_path == "/private/source.xlsx"
    assert "DATABASE_URL" not in plan.environment
    python_path = dict(plan.environment)["PYTHONPATH"].split(":")
    assert site.getsitepackages()[0] in python_path


def test_macos_launcher_executes_the_canonical_profiled_python_path():
    runner = RestrictedSubprocess(project_root=".")
    plan = runner.build_command([sys.executable, "-c", "pass"], input_path="/private/source.xlsx")
    canonical_executable = str(Path(sys.executable).resolve())
    assert plan.command[3] == canonical_executable
    assert f'(allow process-exec (literal "{canonical_executable}"))' in plan.profile


def test_macos_profile_allows_only_the_python_framework_helper():
    helper_dir = Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS"
    if not (helper_dir / "Python").is_file():
        pytest.skip("this Python installation has no framework helper executable")
    profile = RestrictedSubprocess.mac_profile("/repo", None, sys.executable)
    assert f'(allow process-exec (subpath "{helper_dir.resolve()}"))' in profile
    assert f'(path-ancestors "{Path(sys.executable).resolve()}")' in profile


def test_unsupported_platform_fails_closed(tmp_path):
    runner = RestrictedSubprocess(project_root=tmp_path, platform_name="plan9")
    with pytest.raises(IsolationUnavailable, match="restricted_worker_unavailable"):
        runner.run(["anything"], timeout_seconds=1)


def test_darwin_worker_exceeding_memory_cap_is_terminated(monkeypatch, tmp_path):
    runner = RestrictedSubprocess(project_root=tmp_path, platform_name="darwin")
    monkeypatch.setattr(runner, "_memory_usage_bytes", lambda _pid: 2 * 1024**3 + 1, raising=False)
    with pytest.raises(RestrictedWorkerFailed, match="worker_memory_limit_exceeded"):
        runner._run_bounded(["/bin/sleep", "5"], {}, timeout_seconds=6)
