from __future__ import annotations

import os
import selectors
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class IsolationUnavailable(RuntimeError):
    """Raised when the OS cannot provide the required restricted boundary."""


class RestrictedWorkerFailed(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class RestrictedCommand:
    command: tuple[str, ...]
    profile: str
    input_path: str | None
    environment: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RestrictedResult:
    returncode: int
    stdout: bytes


class RestrictedSubprocess:
    """Launch parser code in an OS sandbox with no outbound or inbound network.

    macOS uses Seatbelt (`sandbox-exec`) with a default-deny profile. Linux uses
    Bubblewrap with a new network namespace and a read-only root. Unsupported or
    unavailable isolation fails closed; this class never falls back to a plain
    Python subprocess.
    """

    def __init__(self, project_root: str | Path, *, platform_name: str | None = None,
                 sandbox_exec: str = "/usr/bin/sandbox-exec", bwrap: str | None = None,
                 max_output_bytes: int = 2 * 1024 * 1024,
                 max_memory_bytes: int = 2 * 1024**3):
        self.project_root = Path(project_root).resolve()
        self.platform_name = platform_name or sys.platform
        self.sandbox_exec = sandbox_exec
        self.bwrap = bwrap or shutil.which("bwrap") or ""
        self.max_output_bytes = max_output_bytes
        self.max_memory_bytes = max_memory_bytes

    @staticmethod
    def mac_profile(project_root: str | Path, input_path: str | Path | None,
                    executable: str | Path | None = None, *,
                    read_paths: Sequence[str | Path] = (), write_paths: Sequence[str | Path] = (),
                    executable_paths: Sequence[str | Path] = ()) -> str:
        root = Path(project_root).resolve()
        paths = [
            Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), root / "packages",
            Path("/System/Library"), Path("/usr/lib"), Path("/usr/share/zoneinfo"), Path("/dev"),
        ]
        input_resolved = Path(input_path).resolve() if input_path is not None else None
        paths.extend(item for path in read_paths for item in (Path(path).absolute(), Path(path).resolve()))
        paths.extend(Path(path).resolve() for path in write_paths)
        if input_resolved is not None:
            paths.append(input_resolved)
        escaped = [p.as_posix().replace("\\", "\\\\").replace('"', '\\"') for p in paths]
        project_root_path = root.as_posix().replace("\\", "\\\\").replace('"', '\\"')
        executable_path = Path(executable or sys.executable).resolve().as_posix().replace('"', '\\"')
        framework_helper = Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python"
        process_rules = [f'(allow process-exec (literal "{executable_path}"))']
        process_rules.extend(
            f'(allow process-exec (literal "{Path(path).resolve().as_posix()}"))'
            for path in executable_paths
        )
        metadata_paths = {executable_path, project_root_path, *escaped}
        if input_resolved is not None:
            metadata_paths.add(input_resolved.as_posix())
        metadata_paths.update(Path(path).resolve().as_posix() for path in write_paths)
        if framework_helper.is_file():
            helper_path = framework_helper.resolve().as_posix().replace('"', '\\"')
            helper_directory = framework_helper.parent.resolve().as_posix().replace('"', '\\"')
            metadata_paths.add(helper_path)
            process_rules.append(f'(allow process-exec (subpath "{helper_directory}"))')
        metadata_entries = [f'(path-ancestors "{path}")' for path in sorted(metadata_paths)]
        if input_resolved is not None:
            metadata_entries.append(f'(literal "{input_resolved.as_posix()}")')
        metadata_rules = "(allow file-read-metadata file-test-existence " + " ".join(metadata_entries) + ")"
        read_roots = [Path(path).as_posix().replace('"', '\\"') for path in paths[:7]]
        for path in read_paths:
            read_roots.extend(candidate.as_posix().replace('"', '\\"')
                              for candidate in (Path(path).absolute(), Path(path).resolve()))
        read_rules = " ".join(f'(subpath "{path}")' for path in read_roots)
        read_rules += f' (literal "{project_root_path}")'
        if input_path is not None:
            literal_input = input_resolved.as_posix().replace('"', '\\"')
            read_rules += f' (literal "{literal_input}")'
        write_rules = " ".join(f'(subpath "{Path(path).resolve().as_posix()}")' for path in write_paths)
        return "\n".join((
            "(version 1)",
            '(import "dyld-support.sb")',
            metadata_rules,
            "(deny default)",
            *process_rules,
            "(allow process-fork)",
            "(allow sysctl-read)",
            f"(allow file-read* {read_rules})",
            *( [f"(allow file-write* {write_rules})"] if write_rules else [] ),
            "(deny network*)",
        ))

    def build_command(self, argv: Sequence[str], *, input_path: str | Path | None = None,
                      read_paths: Sequence[str | Path] = (), write_paths: Sequence[str | Path] = (),
                      executable_paths: Sequence[str | Path] = (),
                      environment_overrides: dict[str, str] | None = None) -> RestrictedCommand:
        if not argv or any("\x00" in item for item in argv):
            raise ValueError("invalid_worker_command")
        if os.geteuid() == 0:
            raise IsolationUnavailable("restricted_worker_unavailable:root_worker_forbidden")
        source = str(Path(input_path).resolve()) if input_path is not None else None
        environment = {
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                [str(self.project_root), *(site.getsitepackages() if self.platform_name == "darwin" else [])]
            ),
        }
        if environment_overrides:
            if set(environment_overrides) - {"PATH", "TMPDIR"}:
                raise ValueError("restricted_worker_environment_override_not_allowed")
            environment.update(environment_overrides)
        if self.platform_name == "darwin":
            if not Path(self.sandbox_exec).is_file():
                raise IsolationUnavailable("restricted_worker_unavailable:macos_sandbox_exec_missing")
            canonical_argv = (str(Path(argv[0]).resolve()), *argv[1:])
            executable = canonical_argv[0]
            profile = self.mac_profile(self.project_root, source, executable,
                                       read_paths=read_paths, write_paths=write_paths,
                                       executable_paths=executable_paths)
            command = (self.sandbox_exec, "-p", profile, *canonical_argv)
            return RestrictedCommand(command, profile, source, tuple(sorted(environment.items())))
        if self.platform_name.startswith("linux"):
            if not self.bwrap:
                raise IsolationUnavailable("restricted_worker_unavailable:linux_bwrap_missing")
            if os.geteuid() == 0:
                raise IsolationUnavailable("restricted_worker_unavailable:root_worker_forbidden")
            prefix = Path(sys.prefix).resolve()
            mapped_prefix = "/opt/idw/venv"
            profile = "bwrap --unshare-user --unshare-net --unshare-pid --unshare-ipc; minimal read-only mounts"
            command = [self.bwrap, "--unshare-user", "--unshare-net", "--unshare-pid", "--unshare-ipc",
                       "--new-session", "--die-with-parent", "--uid", str(os.geteuid()),
                       "--gid", str(os.getegid()), "--tmpfs", "/",
                       "--dir", "/tmp", "--dir", "/app", "--dir", "/opt", "--dir", "/opt/idw",
                       "--dir", "/etc", "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
                       "--ro-bind", str(prefix), mapped_prefix,
                       "--ro-bind", str(self.project_root / "packages"), "/app/packages",
                       "--proc", "/proc", "--dev", "/dev", "--chdir", "/tmp",
                       "--clearenv", "--setenv", "HOME", "/nonexistent", "--setenv", "LANG", "C",
                       "--setenv", "LC_ALL", "C", "--setenv", "PATH", f"{mapped_prefix}/bin:/usr/bin:/bin",
                       "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "PYTHONPATH", "/app"]
            cache = Path("/etc/ld.so.cache")
            if cache.is_file():
                command.extend(["--ro-bind", str(cache), str(cache)])
            if source is not None:
                command.extend(["--ro-bind", source, "/tmp/idw-source"])
            for index, path in enumerate(write_paths):
                command.extend(["--dir", f"/tmp/idw-write-{index}", "--bind", str(Path(path).resolve()),
                                f"/tmp/idw-write-{index}"])
            sandbox_argv = list(argv)
            try:
                relative_executable = Path(sandbox_argv[0]).resolve().relative_to(prefix)
                sandbox_argv[0] = f"{mapped_prefix}/{relative_executable.as_posix()}"
            except ValueError:
                pass
            if source is not None:
                sandbox_argv = ["/tmp/idw-source" if arg == source else arg for arg in sandbox_argv]
            command.extend(["--", *sandbox_argv])
            return RestrictedCommand(tuple(command), profile, source, tuple(sorted(environment.items())))
        raise IsolationUnavailable("restricted_worker_unavailable:unsupported_platform")

    def run(self, argv: Sequence[str], *, input_path: str | Path | None = None,
            timeout_seconds: int = 30, read_paths: Sequence[str | Path] = (),
            write_paths: Sequence[str | Path] = (), executable_paths: Sequence[str | Path] = (),
            environment_overrides: dict[str, str] | None = None) -> RestrictedResult:
        if timeout_seconds < 1:
            raise ValueError("timeout_must_be_positive")
        plan = self.build_command(argv, input_path=input_path, read_paths=read_paths,
                                  write_paths=write_paths, executable_paths=executable_paths,
                                  environment_overrides=environment_overrides)
        env = dict(plan.environment)
        profile_file = None
        command = list(plan.command)
        if self.platform_name == "darwin":
            profile_file = tempfile.NamedTemporaryFile(prefix="idw-sandbox-", suffix=".sb", mode="w", delete=False)
            try:
                os.chmod(profile_file.name, 0o600)
                profile_file.write(plan.profile)
                profile_file.close()
                command = [self.sandbox_exec, "-f", profile_file.name, *plan.command[3:]]
            except BaseException:
                profile_file.close()
                Path(profile_file.name).unlink(missing_ok=True)
                raise

        try:
            return self._run_bounded(command, env, timeout_seconds)
        finally:
            if profile_file is not None:
                Path(profile_file.name).unlink(missing_ok=True)

    def _run_bounded(self, command: list[str], env: dict[str, str], timeout_seconds: int) -> RestrictedResult:
        import resource

        def apply_limits() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (timeout_seconds, timeout_seconds + 1))
            # Darwin exposes RLIMIT_AS in Python but its kernel rejects attempts
            # to set it. On macOS the parent enforces the same cap by monitoring
            # resident memory; Linux keeps the kernel-enforced address-space cap.
            if self.platform_name.startswith("linux"):
                resource.setrlimit(resource.RLIMIT_AS, (self.max_memory_bytes, self.max_memory_bytes))
            resource.setrlimit(resource.RLIMIT_FSIZE, (self.max_output_bytes, self.max_output_bytes))
            resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))

        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env, start_new_session=True,
                                   preexec_fn=apply_limits)
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        captured = bytearray()
        deadline = time.monotonic() + timeout_seconds
        next_memory_check = 0.0
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RestrictedWorkerFailed("worker_timeout")
                poll_interval = 0.05 if self.platform_name == "darwin" else 0.25
                ready = selector.select(min(remaining, poll_interval))
                if ready:
                    chunk = os.read(process.stdout.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(process.stdout)
                    else:
                        captured.extend(chunk)
                        if len(captured) > self.max_output_bytes:
                            raise RestrictedWorkerFailed("worker_output_limit_exceeded")
                status = process.poll()
                if status is not None and not selector.get_map():
                    return RestrictedResult(status, bytes(captured))
                if self.platform_name == "darwin" and status is None and time.monotonic() >= next_memory_check:
                    memory_bytes = self._memory_usage_bytes(process.pid)
                    next_memory_check = time.monotonic() + poll_interval
                    if memory_bytes is None:
                        if process.poll() is None:
                            raise RestrictedWorkerFailed("worker_memory_monitor_unavailable")
                    elif memory_bytes > self.max_memory_bytes:
                        raise RestrictedWorkerFailed("worker_memory_limit_exceeded")
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        finally:
            selector.close()

    @staticmethod
    def _memory_usage_bytes(process_group_id: int) -> int | None:
        """Sum RSS for the isolated worker process group on macOS, or return None if it exited."""
        try:
            result = subprocess.run(
                ["/bin/ps", "-axo", "pgid=,rss="], check=True, capture_output=True,
                text=True, timeout=1,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RestrictedWorkerFailed("worker_memory_monitor_unavailable") from error
        total_kib = 0
        found = False
        try:
            for line in result.stdout.splitlines():
                group_text, rss_text = line.split()
                if int(group_text) == process_group_id:
                    total_kib += int(rss_text)
                    found = True
        except (ValueError, TypeError) as error:
            raise RestrictedWorkerFailed("worker_memory_monitor_unavailable") from error
        return total_kib * 1024 if found else None
