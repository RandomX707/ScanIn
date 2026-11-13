"""Stage-by-stage timing of a native XLSX save on the ISOLATED database (in-process, real code, real sandboxed workers)."""
from __future__ import annotations
import functools, io, os, sys, tempfile, time, json
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts/dev"))
DB = "channel360_iso_dev"; STATE = ROOT / ".runtime/iso-stack"
os.environ.update({"PGPASSFILE": str(STATE / "pgpass"), "IDW_DATABASE_URL": f"postgresql+psycopg://channel360_test_app@127.0.0.1:5432/{DB}",
    "IDW_WORKER_DATABASE_URL": f"postgresql+psycopg://channel360_test_worker@127.0.0.1:5432/{DB}", "IDW_MIGRATION_DATABASE_URL": f"postgresql+psycopg://channel360_test_migrator@127.0.0.1:5432/{DB}",
    "IDW_DEVELOPMENT_AUTH_SECRET": (ROOT / ".runtime/dev/development-auth-secret").read_text().strip(), "IDW_ENVIRONMENT": "local", "IDW_AUTH_MODE": "development",
    "IDW_STORAGE_ROOT": str(STATE / "storage-profile"), "IDW_CLAMAV_SOCKET_PATH": "/opt/homebrew/var/run/clamav/clamd.sock", "IDW_WEB_ORIGINS": '["http://127.0.0.1:5180"]'})
from fastapi.testclient import TestClient
import openpyxl
from iso_client import new_tenant, token
from packages.api.app import create_app
from packages.workers.parser import RestrictedParser
from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.verification.independent import IndependentVerifier
import packages.workflow.release as _rel

TIMES = defaultdict(lambda: [0, 0.0])
def wrap(cls, name, label=None):
    fn = getattr(cls, name)
    @functools.wraps(fn)
    def inner(*a, **k):
        t = time.perf_counter()
        try: return fn(*a, **k)
        finally:
            slot = TIMES[label or f"{cls.__name__}.{name}"]; slot[0] += 1; slot[1] += time.perf_counter() - t
    setattr(cls, name, inner)
for m in [n for n in ("inspect", "verify", "verify_derivative", "verify_release") if hasattr(RestrictedParser, n)]: wrap(RestrictedParser, m)
wrap(IndependentVerifier, "verify")
for _n in dir(_rel):
    _c = getattr(_rel, _n)
    if isinstance(_c, type) and _c.__module__ == _rel.__name__:
        for _m in ("freeze", "release"):
            if _m in _c.__dict__: wrap(_c, _m)
for m in ("targets", "unsupported_targets", "preflight", "apply", "_data", "permission_for", "formula_dependents"): wrap(XlsxAdapter, m)

rows, cols = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (6000, 30)
wb = openpyxl.Workbook(); ws = wb.active
for r in range(1, rows + 1): ws.append([f"Row {r}", r, r * 1.5, "x" * 12] + [r + k for k in range(cols - 4)])
path = Path(tempfile.mkdtemp()) / "big.xlsx"; wb.save(path)
app = create_app(); tenant = new_tenant("profile"); h = {"Authorization": f"Bearer {token(tenant)}"}
with TestClient(app) as c:
    t = time.perf_counter(); r = c.put("/api/documents?filename=big.xlsx", content=path.read_bytes(), headers=h); d, v = r.json()["document_id"], r.json()["version_id"]
    c.post(f"/api/documents/{d}/inspect", headers=h); print("import+inspect %.1fs" % (time.perf_counter() - t))
    TIMES.clear()
    pv = c.get(f"/api/documents/{d}/versions/{v}/preview", headers=h).json(); cell = next(x for x in pv["sheets"][0]["rows"][3] if x and x["t"] == "string" and x["e"]) if "sheets" in pv else None
    TIMES.clear()
    t = time.perf_counter()
    r = c.post(f"/api/documents/{d}/versions/{v}/save", headers=h, json={"changes": [{"target_id": cell["id"], "fingerprint": cell["fp"], "value": "edited"}]})
    total = time.perf_counter() - t
print("SAVE %.1fs status %s outcome %s" % (total, r.status_code, r.json().get("outcome") or r.json().get("code")))
for k, (n, s) in sorted(TIMES.items(), key=lambda kv: -kv[1][1]): print(f"  {k:36} calls {n:3}  {s:6.2f}s")
