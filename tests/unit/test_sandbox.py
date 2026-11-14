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
    assert "(deny default)" in p
    assert "(allow default)" not in p
    assert "(deny network*)" in p
    assert "(deny file-write*)" in p
    assert '(allow file-write* (subpath "' in p
    assert "/private/var/folders" not in p and "/private/var/tmp" not in p
    assert '(deny file-read* (subpath "/Users"))' not in p  # default-deny covers the whole filesystem


@live
def test_seatbelt_runtime_isolates_two_jobs_and_restricts_project_storage_and_children(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("requires real macOS Seatbelt runtime")
    job_a = Path(tempfile.mkdtemp(prefix="c360-job-a-", dir="/private/tmp")).resolve()
    job_b = Path(tempfile.mkdtemp(prefix="c360-job-b-", dir="/private/tmp")).resolve()
    external_tmp = Path(tempfile.mkdtemp(prefix="c360-external-tmp-", dir="/private/var/tmp")).resolve()
    credential = Path(tempfile.mkdtemp(prefix="c360-credential-", dir=str(Path.home()))) / "canary"
    credential.parent.chmod(0o700); credential.write_text("not-a-real-credential"); credential.chmod(0o600)
    storage_root = Path(os.environ.get("IDW_STORAGE_ROOT", str(Path(__file__).resolve().parents[2] / ".runtime/iso-final/storage"))).resolve()
    secret_b = job_b / "private.txt"; secret_b.write_text("job-b-only"); secret_b.chmod(0o600)
    tmp_secret = external_tmp / "private.txt"; tmp_secret.write_text("external-temp-only"); tmp_secret.chmod(0o600)
    scratch = job_a / "scratch"; scratch.mkdir(mode=0o700)
    server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen(1); port = server.getsockname()[1]
    try:
        script = f"""import json, os, socket, subprocess, sys
checks = {{}}
def attempt(name, fn):
    try: fn(); checks[name] = True
    except Exception: checks[name] = False
attempt('own_read', lambda: open('own.txt').read())
attempt('own_write', lambda: open('own-write.txt','w').write('ok'))
attempt('job_b_read', lambda: open({str(secret_b)!r}).read())
attempt('job_b_write', lambda: open({str(job_b / 'written.txt')!r},'w').write('bad'))
attempt('external_tmp_read', lambda: open({str(tmp_secret)!r}).read())
attempt('external_tmp_write', lambda: open({str(external_tmp / 'written.txt')!r},'w').write('bad'))
attempt('credential_read', lambda: open({str(credential)!r}).read())
attempt('storage_list', lambda: os.listdir({str(storage_root)!r}))
attempt('project_read', lambda: open({str(Path(__file__).resolve().parents[2] / 'pyproject.toml')!r}).read())
attempt('network', lambda: socket.create_connection(('127.0.0.1', {port}), timeout=1))
child_code = f"print('child-started');\\ntry: open({str(secret_b)!r}).read(); print('child-leaked')\\nexcept Exception: print('child-denied')"
child = subprocess.run([sys.executable, '-I', '-c', child_code], capture_output=True, text=True)
checks['child_denied'] = child.returncode == 0 and 'child-started' in child.stdout and 'child-denied' in child.stdout and 'child-leaked' not in child.stdout
print(json.dumps(checks, sort_keys=True))
"""
        (scratch / "own.txt").write_text("job-a-own")
        os.chmod(scratch / "own.txt", 0o600)
        tool = sb.Tool("python-canary", sys.executable, (str(Path(sys.executable).resolve().parents[1]), sys.prefix, sys.base_prefix))
        result = runner.run(tool, ["-I", "-c", script], sb.SandboxPolicy(scratch=scratch, read_paths=(Path(sys.prefix), Path(sys.base_prefix)), timeout_seconds=30, cpu_seconds=30), capture_stdout=True, capture_stderr=True)
        checks = __import__("json").loads(result.stdout)
        assert checks == {"child_denied": True, "credential_read": False, "external_tmp_read": False, "external_tmp_write": False,
                          "job_b_read": False, "job_b_write": False, "network": False, "own_read": True, "own_write": True,
                          "project_read": False, "storage_list": False}
        assert not (job_b / "written.txt").exists() and not (external_tmp / "written.txt").exists()
    finally:
        server.close()
        import shutil
        shutil.rmtree(job_a, ignore_errors=True); shutil.rmtree(job_b, ignore_errors=True); shutil.rmtree(external_tmp, ignore_errors=True)
        shutil.rmtree(credential.parent, ignore_errors=True)


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


def test_soffice_profile_has_no_shared_temp_grants_and_denies_ip_network():
    tool = sb.resolve_tool("soffice")
    if tool is None or sys.platform != "darwin":
        pytest.skip("LibreOffice/macOS not present")
    scratch = Path(tempfile.mkdtemp(prefix="c360-prof-", dir="/private/tmp")).resolve()
    try:
        profile = sb.SeatbeltRunner().command(tool, [], sb.SandboxPolicy(scratch=scratch))[2]
    finally:
        scratch.rmdir()
    assert "(deny default)" in profile and "(allow default)" not in profile
    assert '(subpath "/private/var/tmp")' not in profile and '(subpath "/private/var/folders")' not in profile and '(subpath "/private/tmp")' not in profile
    assert "file-write*" in profile and "/private/var/folders" not in profile
    assert "OSL_PIPE_" in profile and "(deny network*)" in profile and "unix-socket" in profile and "(allow network* (remote ip)" not in profile


def test_libreoffice_renders_a_real_document_under_live_seatbelt(tmp_path):
    from packages.workers.office_render import SandboxedOfficeRenderer
    if sys.platform != "darwin" or sb.resolve_tool("soffice") is None or not sb.self_test(sb.make_runner())[0]:
        pytest.skip("requires macOS Seatbelt and LibreOffice (reported NOT VERIFIED otherwise)")
    docx = pytest.importorskip("docx")
    document = docx.Document(); document.add_paragraph("sandbox render canary"); path = tmp_path / "canary.docx"; document.save(path)
    pdf = SandboxedOfficeRenderer().render_pdf(path.read_bytes(), ".docx")
    assert pdf.startswith(b"%PDF-") and len(pdf) > 500
