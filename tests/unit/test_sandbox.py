"""Platform-independent confinement: policy, tool registry, runners, fail-closed behaviour and (where the OS provides it) real escape attempts."""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from packages.workers import sandbox as sb

PY = sb.Tool("python", sys.executable, ())
READ = (Path(sys.prefix), Path(sys.base_prefix))
runner = sb.make_runner()
live = pytest.mark.skipif(not runner.available()[0] or not sb.self_test(runner)[0], reason="no working confinement on this host (reported NOT VERIFIED)")


def scratch(prefix="c360sbx-"):
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/private/tmp" if sys.platform == "darwin" else None)).resolve()


def run_py(code, **kw):
    pol = sb.SandboxPolicy(scratch=scratch(), read_paths=READ, **kw)
    return runner.run(PY, ["-I", "-c", code], pol, capture_stdout=True, capture_stderr=True)


# ------------------------------------------------------------------ registry and fail-closed rules (every platform)
def test_only_registered_tools_can_be_resolved():
    with pytest.raises(ValueError):
        resolve = sb.resolve_tool("sh")
    with pytest.raises(ValueError):
        sb.resolve_tool("/bin/sh")
    assert sb.resolve_tool("openssl") is None or sb.resolve_tool("openssl").name == "openssl"


def test_unavailable_runner_never_runs_anything():
    with pytest.raises(sb.SandboxUnavailable):
        sb.UnavailableRunner().run(PY, ["-c", "print(1)"], sb.SandboxPolicy(scratch=scratch()))


def test_confined_runner_fails_closed_when_the_self_test_fails(monkeypatch):
    bad = sb.SandboxStatus("x", "seatbelt", "label", False, False, "confinement leaked or is unusable", {})
    monkeypatch.setattr(sb, "sandbox_status", lambda refresh=False: bad)
    with pytest.raises(sb.SandboxUnavailable) as e:
        sb.confined_runner()
    assert "sandbox_not_verified" in e.value.reason_code


def test_bubblewrap_command_denies_network_and_confines_writes_to_scratch():
    tool = sb.Tool("soffice", "/opt/libreoffice/program/soffice", ("/opt/libreoffice",))
    pol = sb.SandboxPolicy(scratch=scratch(), read_paths=(Path("/etc/hosts"),), env=(("HOME", "/should/be/overridden"),))
    cmd = sb.BubblewrapRunner().command(tool, ["--headless", "x.docx"], pol)
    joined = " ".join(cmd)
    assert "--unshare-all" in cmd and "--die-with-parent" in cmd and "--clearenv" in cmd and "--new-session" in cmd
    assert "--share-net" not in cmd and "--cap-add" not in joined and "--dev-bind" not in cmd
    scratch_bind = [i for i, a in enumerate(cmd) if a == "--bind"]
    assert len(scratch_bind) == 1 and cmd[scratch_bind[0] + 1] == str(pol.scratch.resolve())      # the ONLY writable mount
    assert all(cmd[i + 1] == cmd[i + 2] for i, a in enumerate(cmd) if a == "--ro-bind")
    assert cmd[cmd.index("--") + 1:] == ["/opt/libreoffice/program/soffice", "--headless", "x.docx"]
    env = {cmd[i + 1]: cmd[i + 2] for i, a in enumerate(cmd) if a == "--setenv"}
    assert env["HOME"] == "/should/be/overridden" or env["HOME"] == str(pol.scratch)
    assert set(env) <= {"HOME", "TMPDIR", "PATH", "LANG", "LC_ALL", "OPENSSL_CONF"}


def test_seatbelt_profile_denies_ip_network_and_user_files_and_limits_writes():
    p = sb.SeatbeltRunner.profile(sb.SandboxPolicy(scratch=scratch()))
    assert "(deny network-outbound (remote ip))" in p and "(deny network-bind (local ip))" in p
    assert "(deny file-write*)" in p and '(deny file-read* (subpath "/Users"))' in p
    assert p.count("(allow file-write*") == 1


def test_env_is_an_allowlist_and_runner_refuses_root_and_bad_scratch(tmp_path, monkeypatch):
    env = sb._env(sb.SandboxPolicy(scratch=tmp_path, env=(("LANG", "C"), ("AWS_SECRET_ACCESS_KEY", "x"))))
    assert "AWS_SECRET_ACCESS_KEY" not in env and env["LANG"] == "C" and env["HOME"] == str(tmp_path)
    link = tmp_path / "link"; link.symlink_to(tmp_path)
    with pytest.raises(sb.SandboxUnavailable):
        sb._check_scratch(link)


# ------------------------------------------------------------------ real confinement (needs the OS facility; skipped, and reported NOT VERIFIED, otherwise)
@live
def test_self_test_passes_and_status_says_verified_here():
    status = sb.sandbox_status(refresh=True)
    assert status.verified_here and status.runner == runner.name


@live
def test_reading_other_files_writing_outside_scratch_and_network_are_all_refused(tmp_path):
    home_file = Path(tempfile.mkdtemp(prefix="c360-out-", dir=str(Path.home()))) / "private.txt"; home_file.write_text("secret")
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1); port = srv.getsockname()[1]
    try:
        out = run_py(f"""
import socket
res=[]
for f in (lambda: open({str(home_file)!r}).read(), lambda: open({str(home_file.parent / 'w.txt')!r},'w').write('x'), lambda: socket.create_connection(('127.0.0.1',{port}),timeout=2)):
    try: f(); res.append('LEAK')
    except Exception: res.append('denied')
open('inside.txt','w').write('ok'); res.append('scratch-ok')
print(' '.join(res))""")
        assert out.stdout.decode().split() == ["denied", "denied", "denied", "scratch-ok"]
        assert not (home_file.parent / "w.txt").exists()
    finally:
        srv.close(); subprocess.run(["rm", "-rf", str(home_file.parent)])


@live
def test_environment_of_the_parent_is_not_inherited(monkeypatch):
    monkeypatch.setenv("CHANNEL360_SECRET_CANARY", "leak-me")
    out = run_py("import os; print(','.join(sorted(os.environ)))")
    keys = out.stdout.decode().strip().split(",")
    assert "CHANNEL360_SECRET_CANARY" not in keys and "HOME" in keys


@live
def test_timeout_kills_the_process_group_including_helpers():
    pol = sb.SandboxPolicy(scratch=scratch(), read_paths=READ, timeout_seconds=2)
    code = "import subprocess,time,os; subprocess.Popen(['/bin/sleep','300']); open('pid','w').write(str(os.getpid())); time.sleep(300)"
    start = time.monotonic()
    with pytest.raises(sb.SandboxFailed) as e:
        runner.run(PY, ["-I", "-c", code], pol)
    assert e.value.reason_code == "sandbox_timeout" and time.monotonic() - start < 10
    time.sleep(0.5)
    left = subprocess.run("ps -axo command | grep '[/]bin/sleep 300' | wc -l", shell=True, capture_output=True, text=True).stdout.strip()
    assert left == "0"


@live
@pytest.mark.skipif(sys.platform != "darwin", reason="resident-memory monitor is the macOS enforcement; Linux uses RLIMIT_AS")
def test_memory_ceiling_is_enforced():
    with pytest.raises(sb.SandboxFailed) as e:
        run_py("b=bytearray(600*1024*1024)\nimport time\nfor i in range(0,len(b),4096): b[i]=1\ntime.sleep(5)", memory_bytes=250 * 1024 * 1024, timeout_seconds=30)
    assert e.value.reason_code == "sandbox_memory_limit"


@live
def test_file_size_limit_stops_runaway_output():
    out = run_py("f=open('big','wb')\ntry:\n    f.write(b'x'*5_000_000); f.flush(); print('wrote')\nexcept Exception as e: print('blocked')", max_file_bytes=1_000_000)
    assert b"blocked" in out.stdout or out.returncode != 0


@live
def test_cancel_event_stops_a_running_job():
    import threading
    cancel = threading.Event(); threading.Timer(0.5, cancel.set).start()
    with pytest.raises(sb.SandboxFailed) as e:
        runner.run(PY, ["-I", "-c", "import time; time.sleep(60)"], sb.SandboxPolicy(scratch=scratch(), read_paths=READ, timeout_seconds=60), cancel=cancel)
    assert e.value.reason_code == "sandbox_cancelled"
