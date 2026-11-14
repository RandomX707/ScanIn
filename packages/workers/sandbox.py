"""Platform-independent confinement for external tools that open untrusted documents (LibreOffice, OpenSSL).

Design rules
  * One policy object (`SandboxPolicy`), OS-specific runners behind it. A runner is only used if it is available AND passes a runtime
    self-test that really attempts forbidden actions from inside the confinement (read a private file, write outside the scratch
    directory, open a network connection). A runner that is missing or fails its self-test makes the tool UNAVAILABLE. There is never a
    fallback that runs untrusted documents unconfined.
  * Only tools in a small registry can be run (`resolve_tool`): callers cannot pass an arbitrary executable, and no shell is involved.
  * Every run has: a private scratch directory as the only writable place, an allow-listed environment, CPU / file-size / open-file
    limits, a wall-clock timeout, a resident-memory ceiling (monitored on macOS, kernel address-space limit on Linux), process-group
    cleanup, and no network.

Support status (what is actually verified is stated by `SandboxStatus`, not assumed):
  * macOS  - Seatbelt (`sandbox-exec`): implemented and self-tested here.
  * Linux  - Bubblewrap (`bwrap`): implemented; it is reported VERIFIED only on a host where the self-test passes. It has NOT been run in
             the development environment (macOS), so repository status for Linux is "NOT VERIFIED".
  * Other  - unsupported: the tool is reported unavailable.
"""
from __future__ import annotations

import os
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
ENV_ALLOW = ("LANG", "LC_ALL", "PATH", "HOME", "TMPDIR")


class SandboxUnavailable(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class SandboxFailed(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class SandboxPolicy:
    """What a confined process may do. Network is ALWAYS denied; there is no switch for it."""
    scratch: Path                                     # the only writable directory (and always readable)
    read_paths: tuple[Path, ...] = ()                 # extra read-only paths (for example a trust file); never writable
    timeout_seconds: int = 120
    cpu_seconds: int = 180
    max_file_bytes: int = 128 * 1024 * 1024
    max_open_files: int = 2048
    memory_bytes: int = 3 * 1024**3
    max_processes: int = 256                          # enforced where the OS allows it (Linux)
    env: tuple[tuple[str, str], ...] = ()             # extra environment entries (only keys in ENV_ALLOW are accepted)


@dataclass(frozen=True)
class SandboxResult:
    returncode: int
    stdout: bytes
    stderr: bytes = b""


@dataclass(frozen=True)
class Tool:
    name: str
    path: str
    read_roots: tuple[str, ...] = ()                  # installation directories the tool needs to read (bind-mounted read-only on Linux)
    seatbelt_write_regex: tuple[str, ...] = ()        # macOS only: exact-pattern files outside the scratch directory the tool must create (never a directory grant)
    seatbelt_unix_sockets: bool = False               # macOS only: local (AF_UNIX) sockets; IP networking stays denied
    seatbelt_mach_lookup: bool = False                # macOS only: GUI-framework tools (LibreOffice) need Mach service lookups to start at all
    seatbelt_read_paths: tuple[str, ...] = ()         # macOS only: additional read-only system locations


# ---------------------------------------------------------------------------------------------------------------- tool registry
def _first_file(candidates: Sequence[str | None]) -> str | None:
    for item in candidates:
        if item and Path(item).is_file():
            return str(Path(item).resolve())
    return None


def _openssl_ok(path: str) -> bool:
    try:
        return subprocess.run([path, "version"], capture_output=True, text=True, timeout=5).stdout.startswith("OpenSSL")
    except (OSError, subprocess.SubprocessError):
        return False                                   # LibreSSL has no `cms`; only real OpenSSL is accepted


def resolve_tool(name: str) -> Tool | None:
    """The ONLY way to obtain something runnable. Unknown names raise; nothing is looked up from request data."""
    if name == "soffice":
        found = _first_file([os.environ.get("IDW_SOFFICE_PATH"), "/Applications/LibreOffice.app/Contents/MacOS/soffice", shutil.which("soffice"),
                             "/usr/lib/libreoffice/program/soffice", "/opt/libreoffice/program/soffice"])
        if not found:
            return None
        if ".app/" in found:
            roots = [found.split(".app/")[0] + ".app"]            # the whole application bundle: the loader resolves the bundle root, not only Contents
        else:
            roots = [str(Path(found).parents[1])] if "/program/" in found else [str(Path(found).parent)]
        roots += [p for p in ("/usr/lib/libreoffice", "/usr/share/libreoffice", "/opt/libreoffice") if Path(p).is_dir()]
        return Tool("soffice", found, tuple(dict.fromkeys(roots)),
                    seatbelt_write_regex=(rf"^/private/var/tmp/OSL_PIPE_{os.getuid()}_SingleOfficeIPC_[0-9a-f]+$",),   # LibreOffice's single-instance IPC pipe: fixed name, not a directory grant
                    seatbelt_unix_sockets=True, seatbelt_mach_lookup=True,
                    seatbelt_read_paths=tuple(p for p in ("/Library/Fonts",) if Path(p).exists()))
    if name == "qpdf":
        found = _first_file([os.environ.get("IDW_QPDF_PATH"), shutil.which("qpdf"), "/opt/homebrew/bin/qpdf", "/usr/local/bin/qpdf", "/usr/bin/qpdf"])
        if not found:
            return None
        real = Path(found).resolve()
        brew = Path("/opt/homebrew")
        roots = [str(p) for p in (brew / "Cellar", brew / "opt", brew / "lib") if p.is_dir()] if str(real).startswith(str(brew)) else [str(real.parents[1])]
        return Tool("qpdf", str(real), tuple(roots))
    if name == "openssl":
        for candidate in (os.environ.get("IDW_OPENSSL_PATH"), "/opt/homebrew/bin/openssl", "/usr/local/bin/openssl", shutil.which("openssl"), "/usr/bin/openssl"):
            if candidate and Path(candidate).is_file() and _openssl_ok(candidate):
                real = str(Path(candidate).resolve())
                return Tool("openssl", real, (str(Path(real).parents[1]),))
        return None
    raise ValueError("unknown_tool")


# ---------------------------------------------------------------------------------------------------------------- shared run loop
def _limits(policy: SandboxPolicy):
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (policy.cpu_seconds, policy.cpu_seconds + 2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (policy.max_file_bytes, policy.max_file_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (policy.max_open_files, policy.max_open_files))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (policy.memory_bytes, policy.memory_bytes))
            resource.setrlimit(resource.RLIMIT_NPROC, (policy.max_processes, policy.max_processes))
    return apply


def _env(policy: SandboxPolicy) -> dict[str, str]:
    scratch = str(policy.scratch)
    env = {"HOME": scratch, "TMPDIR": scratch, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "OPENSSL_CONF": "/dev/null"}
    for key, value in policy.env:
        if key in ENV_ALLOW:
            env[key] = value
    return env


def _check_scratch(scratch: Path) -> Path:
    resolved = scratch.resolve(strict=True)
    if scratch.is_symlink() or not resolved.is_dir():
        raise SandboxUnavailable("scratch_invalid")
    os.chmod(resolved, 0o700)
    return resolved


def _rss_bytes(pgid: int) -> int | None:
    try:
        out = subprocess.run(["/bin/ps", "-axo", "pgid=,rss="], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    total = 0; found = False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) == pgid:
            total += int(parts[1]) * 1024; found = True
    return total if found else None


def _supervise(command: list[str], policy: SandboxPolicy, cwd: Path, cancel: threading.Event | None, capture_stdout: bool, capture_stderr: bool = False) -> SandboxResult:
    process = subprocess.Popen(command, cwd=str(cwd), env=_env(policy), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                               stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL, start_new_session=True, preexec_fn=_limits(policy))
    deadline = time.monotonic() + policy.timeout_seconds
    chunks: list[bytes] = []
    reader = None
    if capture_stdout and process.stdout is not None:
        def drain() -> None:
            assert process.stdout is not None
            total = 0
            for block in iter(lambda: process.stdout.read(65536), b""):    # type: ignore[union-attr]
                total += len(block)
                if total <= 1024 * 1024:
                    chunks.append(block)
        reader = threading.Thread(target=drain, daemon=True); reader.start()
    err_chunks: list[bytes] = []
    err_reader = None
    if capture_stderr and process.stderr is not None:
        def drain_err() -> None:
            assert process.stderr is not None
            total = 0
            for block in iter(lambda: process.stderr.read(8192), b""):      # type: ignore[union-attr]
                total += len(block)
                if total <= 64 * 1024:
                    err_chunks.append(block)
        err_reader = threading.Thread(target=drain_err, daemon=True); err_reader.start()
    try:
        next_mem = 0.0
        while process.poll() is None:
            if cancel is not None and cancel.is_set():
                raise SandboxFailed("sandbox_cancelled")
            if time.monotonic() > deadline:
                raise SandboxFailed("sandbox_timeout")
            if sys.platform == "darwin" and time.monotonic() >= next_mem:
                next_mem = time.monotonic() + 0.25
                rss = _rss_bytes(process.pid)
                if rss is not None and rss > policy.memory_bytes:
                    raise SandboxFailed("sandbox_memory_limit")
            time.sleep(0.02)
        for t in (reader, err_reader):
            if t is not None:
                t.join(2)
        return SandboxResult(process.returncode, b"".join(chunks), b"".join(err_chunks))
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)         # reap helpers (for example soffice.bin) even after a clean exit
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------------------------------------------- runners
class Runner:
    name = "base"
    platform_label = ""

    def available(self) -> tuple[bool, str | None]:
        raise NotImplementedError

    def command(self, tool: Tool, args: Sequence[str], policy: SandboxPolicy) -> list[str]:
        raise NotImplementedError

    def run(self, tool: Tool, args: Sequence[str], policy: SandboxPolicy, *, cancel: threading.Event | None = None, capture_stdout: bool = False, capture_stderr: bool = False) -> SandboxResult:
        ok, reason = self.available()
        if not ok:
            raise SandboxUnavailable(reason or "sandbox_unavailable")
        if os.geteuid() == 0:
            raise SandboxUnavailable("root_worker_forbidden")
        scratch = _check_scratch(policy.scratch)
        if any("\x00" in a for a in args):
            raise ValueError("invalid_argument")
        return _supervise(self.command(tool, args, policy), policy, scratch, cancel, capture_stdout, capture_stderr)


class SeatbeltRunner(Runner):
    name = "seatbelt"
    platform_label = "macOS Seatbelt (sandbox-exec)"

    def available(self) -> tuple[bool, str | None]:
        if sys.platform != "darwin":
            return False, "seatbelt_requires_macos"
        return (True, None) if Path(SANDBOX_EXEC).is_file() else (False, "sandbox_exec_missing")

    @staticmethod
    def _chain(path: str | Path) -> list[Path]:
        """Every hop of a symlink chain (each hop's own path, then its target), so an interpreter reached through
        venv/Homebrew-style links can be read, traversed and executed without granting the directories around it."""
        hops: list[Path] = []
        pending = [Path(os.path.abspath(path))]
        while pending and len(hops) < 64:
            current = pending.pop()
            if current in hops:
                continue
            hops.append(current)
            for index in range(1, len(current.parts) + 1):             # the file itself and every directory component on the way
                prefix = Path(*current.parts[:index])
                if prefix.is_symlink():
                    pending.append(Path(os.path.abspath(Path(os.path.realpath(prefix.parent)) / os.readlink(prefix))))
                    for form in (prefix, Path(os.path.realpath(prefix.parent)) / prefix.name):      # the link as spelled and where it really lives
                        if form not in hops:
                            hops.append(form)
        return hops

    @staticmethod
    def profile(policy: SandboxPolicy, links: Sequence[Path] = (), tool: Tool | None = None) -> str:
        q = lambda p: str(Path(p).resolve()).replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
        reads = [f'(subpath "{q(policy.scratch)}")'] + [f'(literal "{q(p)}")' if Path(p).is_file() else f'(subpath "{q(p)}")' for p in policy.read_paths]
        system_roots = tuple(p for p in ("/System/Library", "/usr", "/Library/Frameworks") if Path(p).exists())
        reads.extend(f'(subpath "{q(p)}")' for p in system_roots)
        exec_roots = list(system_roots)
        exec_roots.extend(str(Path(p).resolve()) for p in policy.read_paths if Path(p).is_dir())
        if tool is not None:
            reads.extend(f'(subpath "{q(p)}")' for p in tool.seatbelt_read_paths)
        extra_rules: list[str] = []
        if tool is not None:
            if tool.seatbelt_write_regex:
                extra_rules.append('(allow file-write* (literal "/private/var/tmp"))')       # the directory entry only (not its children); the files are the patterns below
            extra_rules.extend(f'(allow file-write* (regex #"{r}"))' for r in tool.seatbelt_write_regex)
            if tool.seatbelt_unix_sockets:
                extra_rules.append("(allow network* (local unix-socket) (remote unix-socket))")
            if tool.seatbelt_mach_lookup:
                extra_rules.append("(allow mach*)")
        reads.extend(f'(literal "{p}")' for p in ("/etc", "/var", "/tmp") if Path(p).is_symlink())      # the firmlinks themselves (not their targets)
        link_literals = [f'(literal "{str(l).replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}")' for l in links]
        reads.extend(link_literals)
        return "\n".join((
            "(version 1)", "(deny default)", "(allow process*)", "(allow sysctl-read)",
            "(deny network*)",
            "(deny file-write*)",
            f'(allow file-write* (subpath "{q(policy.scratch)}") (literal "/dev/null"))',
            *extra_rules,
            f"(allow file-read* {' '.join(reads)} (literal \"/dev/null\") (literal \"/dev/urandom\") (literal \"/\"))",
            "(allow file-read-metadata " + " ".join(f'(path-ancestors "{q(p)}")' for p in (policy.scratch, *policy.read_paths)) + " " + " ".join(f'(path-ancestors "{l}")' for l in links) + ")",
            "(allow process-exec " + " ".join(f'(subpath "{q(p)}")' for p in exec_roots) + " " + " ".join(link_literals) + ")"))

    def command(self, tool: Tool, args: Sequence[str], policy: SandboxPolicy) -> list[str]:
        extra = SandboxPolicy(scratch=policy.scratch, read_paths=(*policy.read_paths, *(Path(p) for p in tool.read_roots), Path(tool.path)))
        return [SANDBOX_EXEC, "-p", self.profile(extra, self._chain(tool.path), tool), tool.path, *args]


class BubblewrapRunner(Runner):
    """Linux: new user/pid/ipc/uts/net/cgroup namespaces, read-only system tree, private /tmp and scratch. NOT VERIFIED on the development host."""
    name = "bubblewrap"
    platform_label = "Linux Bubblewrap (bwrap)"
    SYSTEM_RO = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/fonts", "/etc/alternatives", "/etc/ssl", "/etc/ld.so.cache", "/usr/share/fonts")

    def available(self) -> tuple[bool, str | None]:
        if not sys.platform.startswith("linux"):
            return False, "bubblewrap_requires_linux"
        return (True, None) if shutil.which("bwrap") else (False, "bwrap_missing")

    def command(self, tool: Tool, args: Sequence[str], policy: SandboxPolicy) -> list[str]:
        scratch = str(Path(policy.scratch).resolve())
        cmd = [shutil.which("bwrap") or "bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
               "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        seen: set[str] = set()
        for ro in (*self.SYSTEM_RO, *tool.read_roots, *map(str, policy.read_paths)):
            if ro not in seen and Path(ro).exists():
                seen.add(ro); cmd += ["--ro-bind", ro, ro]
        cmd += ["--bind", scratch, scratch, "--chdir", scratch]
        for key, value in _env(policy).items():
            cmd += ["--setenv", key, value]
        return [*cmd, "--", tool.path, *args]


class UnavailableRunner(Runner):
    name = "unavailable"
    platform_label = "no confinement available on this platform"

    def available(self) -> tuple[bool, str | None]:
        return False, "unsupported_platform"


def make_runner() -> Runner:
    if sys.platform == "darwin":
        return SeatbeltRunner()
    if sys.platform.startswith("linux"):
        return BubblewrapRunner()
    return UnavailableRunner()


# ---------------------------------------------------------------------------------------------------------------- self-test
@dataclass(frozen=True)
class SandboxStatus:
    platform: str
    runner: str
    runner_label: str
    available: bool
    verified_here: bool
    detail: str
    tools: dict[str, bool] = field(default_factory=dict)


def self_test(runner: Runner) -> tuple[bool, str]:
    """Run a canary INSIDE the confinement that tries forbidden actions; passes only if every one is refused and the allowed one works."""
    ok, reason = runner.available()
    if not ok:
        return False, reason or "unavailable"
    outside = Path(tempfile.mkdtemp(prefix="channel360-canary-out-", dir=str(Path.home()) if Path.home().is_dir() and os.access(Path.home(), os.W_OK) else None))      # a private place the sandbox must not read or write
    secret = outside / "secret.txt"; secret.write_text("canary")
    try:
        server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen(1); port = server.getsockname()[1]
    except OSError:
        shutil.rmtree(outside, ignore_errors=True)
        return False, "network_canary_setup_unavailable"
    scratch = Path(tempfile.mkdtemp(prefix="channel360-canary-", dir="/private/tmp" if sys.platform == "darwin" else None)).resolve()
    try:
        script = ("import socket,sys\n"
                  "r={}\n"
                  f"try: open({str(secret)!r}).read(); r['read']=1\nexcept Exception: r['read']=0\n"
                  f"try: open({str(outside / 'written.txt')!r},'w').write('x'); r['write']=1\nexcept Exception: r['write']=0\n"
                  f"try: s=socket.create_connection(('127.0.0.1',{port}),timeout=2); r['net']=1\nexcept Exception: r['net']=0\n"
                  "try: open('allowed.txt','w').write('ok'); r['scratch']=1\nexcept Exception: r['scratch']=0\n"
                  "print(r['read'],r['write'],r['net'],r['scratch'])\n")
        tool = Tool("python-canary", sys.executable, (str(Path(sys.executable).resolve().parents[1]), sys.prefix, sys.base_prefix))
        result = runner.run(tool, ["-I", "-c", script], SandboxPolicy(scratch=scratch, read_paths=(Path(sys.prefix), Path(sys.base_prefix)), timeout_seconds=30, cpu_seconds=30, memory_bytes=2 * 1024**3), capture_stdout=True)
        parts = result.stdout.decode(errors="ignore").split()
        if parts == ["0", "0", "0", "1"] and not (outside / "written.txt").exists():
            return True, "all forbidden actions refused; scratch writable"
        return False, f"confinement leaked or is unusable (read,write,net,scratch={parts})"
    except (SandboxUnavailable, SandboxFailed) as error:
        return False, error.reason_code
    finally:
        server.close(); shutil.rmtree(outside, ignore_errors=True); shutil.rmtree(scratch, ignore_errors=True)


_status_cache: dict[str, SandboxStatus] = {}


def sandbox_status(refresh: bool = False) -> SandboxStatus:
    """Cached runtime verdict used by the API. `verified_here` is true only when the self-test passed on THIS host."""
    if refresh or "s" not in _status_cache:
        runner = make_runner()
        ok, detail = self_test(runner)
        available, reason = runner.available()
        _status_cache["s"] = SandboxStatus(sys.platform, runner.name, runner.platform_label, available and ok, ok, detail if not available else detail,
                                           {name: resolve_tool(name) is not None for name in ("soffice", "openssl")})
    return _status_cache["s"]


def confined_runner() -> Runner:
    """The runner to use for untrusted documents: only if it passed the self-test on this host, otherwise unavailable (fail closed)."""
    status = sandbox_status()
    if not status.verified_here:
        raise SandboxUnavailable(f"sandbox_not_verified:{status.detail}")
    return make_runner()
