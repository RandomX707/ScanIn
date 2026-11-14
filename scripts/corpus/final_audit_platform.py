#!/usr/bin/env python3
"""Independent platform audit: ZIP integrity, signed-PDF restrictions, tenant isolation, immutability, authorization.
Private output .private/audit/platform.json. Console prints check names/booleans only."""
import hashlib, io, json, os, subprocess, sys, urllib.parse, urllib.request, zipfile
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
API = os.environ.get("CHANNEL360_API", "http://127.0.0.1:8010")
OUT = ROOT / ".private/audit"; OUT.mkdir(parents=True, exist_ok=True)
MAN = json.load(open(ROOT / ".private/inventory/manifest.json"))
os.environ.setdefault("PGPASSFILE", str(Path.home() / ".channel360-pgpass"))


def tenant():
    t = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/dev/issue_iso_token.py")], capture_output=True, text=True, check=True).stdout)
    return t, {"Authorization": f"Bearer {t['token']}"}


def call(h, m, p, b=None, ct="application/octet-stream", raw=False):
    r = urllib.request.Request(API + p, data=b, method=m, headers={**h, "Content-Type": ct})
    try:
        with urllib.request.urlopen(r, timeout=600) as x:
            d = x.read(); return x.status, (d if raw else json.loads(d or b"null"))
    except urllib.error.HTTPError as e:
        d = e.read()
        try: return e.code, json.loads(d)
        except Exception: return e.code, d


R = {}
def check(name, ok, detail=""):
    R[name] = {"ok": bool(ok), "detail": str(detail)[:200]}
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if not ok else ""))


def sha(b): return hashlib.sha256(b).hexdigest()
ta, ha = tenant(); tb, hb = tenant()

# ------------------------------------------------------------------ ZIP integrity (tenant A)
uniq = {o["sha256"]: o for o in MAN["occurrences"]}
all_names = {}                       # (archive, path) -> sha
for o in MAN["occurrences"]:
    if o["source"].endswith(".zip"): all_names[(o["source"], o["path"])] = o["sha256"]
for z in ("QCR.zip", "RFQ.zip"):
    s, rep = call(ha, "PUT", f"/api/archives?filename={urllib.parse.quote(z)}", (ROOT / "Policy Copies" / z).read_bytes())
    check(f"{z}: HTTP 200 and every entry accounted for", s == 200 and rep["summary"]["entries"] == (7 if z == "QCR.zip" else 17) == len(rep["entries"]), rep["summary"] if s == 200 else s)
    zf = zipfile.ZipFile(ROOT / "Policy Copies" / z)
    check(f"{z}: entry paths exactly equal the archive's stored names", sorted(e["path"] for e in rep["entries"]) == sorted(i.filename for i in zf.infolist() if not i.is_dir()))
    check(f"{z}: reported sha256 equals each entry's real bytes", all(e["sha256"] == sha(zf.read(e["path"])) for e in rep["entries"]))
items = call(ha, "GET", "/api/folders")[1]["items"]
files = {}
for f in items:
    v1 = f["versions"][0]["version_id"]
    files[f["document_id"]] = sha(call(ha, "GET", f"/api/documents/{f['document_id']}/versions/{v1}/file", raw=True)[1])
check("tenant A holds exactly the 17 unique documents", len(items) == 17 and set(files.values()) == set(uniq), f"{len(items)} docs")
check("no duplicate registration (no two documents share original bytes)", len(set(files.values())) == len(files))
check("every stored original is byte-identical to its source entry", all(h in uniq for h in files.values()))
src_ok = True
for f in items:
    p = (f.get("source") or {}).get("path"); a = (f.get("source") or {}).get("archive")
    if not p or all_names.get((a, p)) != files[f["document_id"]]: src_ok = False
check("each document's recorded archive path points at identical bytes", src_ok)
prov = 0
for f in items:
    ev = call(ha, "GET", f"/api/documents/{f['document_id']}/audit")[1]["items"]
    prov += sum(1 for e in ev if e["action"] == "document.archive_provenance")
check("provenance events = 24 occurrences (7 + 17), duplicates linked not re-added", prov == 24, prov)

# hostile + failure reporting
subprocess.run([sys.executable, str(ROOT / "scripts/corpus/make_hostile_zip.py"), str(OUT / "h.zip")], check=True)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z_:
    z_.writestr("ok/good.csv", "a,b\n1,2\n"); z_.writestr("bad/corrupt.pdf", b"%PDF-1.4\n garbage not a pdf"); z_.writestr("bad/also.xlsx", b"PK\x03\x04 not a workbook")
s, rep = call(ha, "PUT", "/api/archives?filename=failures.zip", buf.getvalue())
st = {e["path"]: e["status"] for e in rep["entries"]}
check("individual failures are reported accurately (good imported, corrupt PDF/XLSX not silently ok)", st["ok/good.csv"] == "imported" and st["bad/corrupt.pdf"] in ("failed", "blocked") and st["bad/also.xlsx"] in ("failed", "blocked"), st)
folders_after = {f["name"] for f in call(ha, "GET", "/api/folders")[1]["items"]}
check("failed children left no ready document", "corrupt.pdf" not in folders_after and "also.xlsx" not in folders_after and "good.csv" in folders_after)
s, rep = call(ha, "PUT", "/api/archives?filename=h.zip", (OUT / "h.zip").read_bytes())
leaked = [str(p) for base in (ROOT, ROOT.parent, Path("/tmp")) for p in base.glob("evil*.csv")] + [str(p) for p in ROOT.glob("**/evil.csv") if "node_modules" not in str(p) and ".venv" not in str(p)][:3]
check("path traversal wrote nothing outside storage (no evil.csv on disk)", not leaked and {e["path"]: e["reason"] for e in rep["entries"]}["../evil.csv"] == "unsafe_path", leaked)

# ------------------------------------------------------------------ signed-PDF restrictions (tenant B)
for name, want in (("Health Policy copy.pdf", "signed"), ("Motor  Policy.pdf", "signed"), ("Marine Policy copy.pdf", "signature_range_mismatch")):
    data = (ROOT / "Policy Copies" / name).read_bytes()
    s, reg = call(hb, "PUT", "/api/documents?filename=" + urllib.parse.quote(name), data); d, v = reg["document_id"], reg["version_id"]; call(hb, "POST", f"/api/documents/{d}/inspect")
    pv = call(hb, "GET", f"/api/documents/{d}/versions/{v}/preview")[1]
    reason = pv["edit"]["reason"] or ""
    check(f"{name[:12]}: signature classified '{want}', validated=False, not editable", (pv.get("signature") or {}).get("status") == want and pv["signature"]["validated"] is False and pv["edit"]["mode"] == "none", pv.get("signature"))
    check(f"{name[:12]}: reason never claims the signature stays valid", "valid" not in reason.lower().replace("invalid", "") or "break" in reason.lower() or "can't be confirmed" in reason.lower(), reason[:60])
    runs = call(hb, "GET", f"/api/documents/{d}/versions/{v}/pdf-text?page=1")[1]["runs"]
    check(f"{name[:12]}: every text run locked", all(not r["e"] for r in runs) and len(runs) > 0)
    rid = runs[0]["id"]
    s1, b1 = call(hb, "POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"pdf_edits": [{"id": rid, "old": runs[0]["text"], "new": runs[0]["text"][:-1]}]}).encode(), "application/json")
    s2, b2 = call(hb, "POST", f"/api/documents/{d}/versions/{v}/pdf-edit/preview", json.dumps({"pdf_edits": [{"id": rid, "old": runs[0]["text"], "new": runs[0]["text"][:-1]}]}).encode(), "application/json")
    s3, b3 = call(hb, "POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"changes": [{"target_id": "x", "fingerprint": "0" * 64, "value": "y"}]}).encode(), "application/json")
    check(f"{name[:12]}: save, preview and native-patch attempts all refused", s1 == 409 and s2 == 409 and s3 >= 400, (s1, s2, s3))
    fold = next(f for f in call(hb, "GET", "/api/folders")[1]["items"] if f["document_id"] == d)
    check(f"{name[:12]}: no derivative exists; original byte-identical", len(fold["versions"]) == 1 and sha(call(hb, "GET", f"/api/documents/{d}/versions/{v}/file", raw=True)[1]) == sha(data))
    R.setdefault("_signed_ids", {})[name] = (d, v)

# ------------------------------------------------------------------ tenant isolation (B cannot touch A)
adoc = items[0]["document_id"]; aver = items[0]["versions"][0]["version_id"]
paths = [("GET", f"/api/documents/{adoc}/versions/{aver}/file"), ("GET", f"/api/documents/{adoc}/versions/{aver}/preview"), ("GET", f"/api/documents/{adoc}/versions/{aver}/chunks"),
         ("GET", f"/api/documents/{adoc}/versions/{aver}/pdf-text?page=1"), ("GET", f"/api/documents/{adoc}/audit"), ("POST", f"/api/documents/{adoc}/inspect"),
         ("POST", f"/api/documents/{adoc}/versions/{aver}/save"), ("POST", f"/api/documents/{adoc}/versions/{aver}/pdf-edit/preview"), ("GET", f"/api/documents/{adoc}/versions/{aver}/download")]
codes = {}
for m, p in paths:
    codes[p.split("/")[-1][:18]] = call(hb, m, p, b=(b"{}" if m == "POST" else None), ct="application/json")[0]
check("tenant B gets 404/422 (never data) for every route on tenant A's document", all(c in (404, 422) for c in codes.values()), codes)
check("tenant B's folder list excludes tenant A's documents", adoc not in {f["document_id"] for f in call(hb, "GET", "/api/folders")[1]["items"]})
ida = {f["document_id"] for f in call(ha, "GET", "/api/folders")[1]["items"]}; idb = {f["document_id"] for f in call(hb, "GET", "/api/folders")[1]["items"]}
check("tenant A's and tenant B's document sets are disjoint", not (ida & idb) and len(idb) == 3, (len(ida), len(idb)))

# ------------------------------------------------------------------ authorization
check("no token -> 401", call({}, "GET", "/api/folders")[0] == 401)
check("garbage token -> 401", call({"Authorization": "Bearer abc.def.ghi"}, "GET", "/api/folders")[0] == 401)
import jwt
forged = jwt.encode({"sub": "x", "tenant_id": ta["tenant_id"], "role": "OWNER", "iss": "idw-local-development", "aud": "idw-api", "exp": 4102444800}, "wrong-secret-wrong-secret-wrong-secret!!", algorithm="HS256")
check("token signed with a different key -> 401", call({"Authorization": f"Bearer {forged}"}, "GET", "/api/folders")[0] == 401)
secret = (ROOT / ".runtime/dev/development-auth-secret").read_text().strip()
bad_role = jwt.encode({"sub": "x", "tenant_id": ta["tenant_id"], "role": "VIEWER", "iss": "idw-local-development", "aud": "idw-api", "exp": 4102444800}, secret, algorithm="HS256")
check("validly signed token with a non-OWNER role -> rejected", call({"Authorization": f"Bearer {bad_role}"}, "GET", "/api/folders")[0] in (401, 403))

# ------------------------------------------------------------------ immutability (database level, app role, tenant A context)
from sqlalchemy import create_engine, text
from packages.api.auth import AuthenticatedIdentity
from packages.api.db_context import api_tenant_transaction
from packages.audit.chain import verify_chain
eng = create_engine("postgresql+psycopg://channel360_test_app@127.0.0.1:5432/channel360_test")
ident = AuthenticatedIdentity(subject="audit", tenant_id=UUID(ta["tenant_id"]), role="OWNER")
def attempt(sql, **kw):
    try:
        with eng.connect() as c, api_tenant_transaction(c, ident):
            r = c.execute(text(sql), kw)
            return f"executed rows={r.rowcount}"
    except Exception as e:
        return "REJECTED:" + type(e).__name__
ver = items[0]["versions"][0]["version_id"]
out = {
    "update version hash": attempt("UPDATE document_versions SET sha256=:h WHERE id=:v", h="0" * 64, v=ver),
    "delete version": attempt("DELETE FROM document_versions WHERE id=:v", v=ver),
    "update file hash": attempt("UPDATE files SET sha256=:h WHERE tenant_id=:t", h="0" * 64, t=ta["tenant_id"]),
    "delete file row": attempt("DELETE FROM files WHERE tenant_id=:t", t=ta["tenant_id"]),
    "update audit payload": attempt("UPDATE audit_events SET payload='{}'::jsonb WHERE tenant_id=:t", t=ta["tenant_id"]),
    "delete audit events": attempt("DELETE FROM audit_events WHERE tenant_id=:t", t=ta["tenant_id"]),
}
effective = {k: v for k, v in out.items() if v.startswith("executed rows=") and v != "executed rows=0"}
check("database refuses UPDATE/DELETE of versions, files and audit events", not effective, out)
with eng.connect() as c, api_tenant_transaction(c, ident):
    chain_a = verify_chain(c, ident.tenant_id)
check("audit hash chain still verifies after the mutation attempts", tuple(chain_a) == (True, None), chain_a)
other = AuthenticatedIdentity(subject="audit", tenant_id=UUID(tb["tenant_id"]), role="OWNER")
with eng.connect() as c, api_tenant_transaction(c, other):
    seen = c.execute(text("SELECT count(*) FROM documents WHERE tenant_id=:t"), {"t": ta["tenant_id"]}).scalar_one()
    seen_all = c.execute(text("SELECT count(*) FROM document_versions")).scalar_one()
check("row-level security: tenant B's database session sees none of tenant A's rows", seen == 0 and seen_all == 3, (seen, seen_all))
stor = ROOT / ".runtime/test-stack/storage"
modes = [oct(p.stat().st_mode & 0o777) for p in list(stor.rglob("*"))[:200] if p.is_file() and ".staging" not in str(p) and ".quarantine" not in str(p)]
check("stored originals are not world-writable", all(int(m, 8) & 0o022 == 0 for m in modes), sorted(set(modes)))

json.dump({k: v for k, v in R.items() if not k.startswith("_")}, open(OUT / "platform.json", "w"), indent=1)
print("SUMMARY", sum(v["ok"] for k, v in R.items() if not k.startswith("_")), "pass /", len([k for k in R if not k.startswith("_")]), "checks")
