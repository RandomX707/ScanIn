"""Real-PostgreSQL verification of migration 0008 (version lineage class) on a disposable ISOLATED database (channel360_iso_*).
Phases (run in order; the driver shell script restarts the isolated API between `api` calls):
  seed     downgrade to 0007 (refuses non-isolated databases), then insert historical data as it looked before 0008
  migrate  alembic upgrade 0008
  sql      verify schema, constraint and backfill classifications straight from the database
  churn    append >1000 audit events for the tenant
  api      verify classifications through the running isolated API (history), incl. cross-tenant isolation
  rollback downgrade 0007 and upgrade again; classifications must be reproduced exactly
Prints counts and verdicts only; no document content is involved."""
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL
sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[2]
DB = os.environ.get("C360_ISO_DB", "")
if not DB.startswith("channel360_iso_") or DB == "channel360_test":
    raise SystemExit("REFUSED: needs C360_ISO_DB=channel360_iso_*")
os.environ["PGPASSFILE"] = str(ROOT / f".runtime/iso-stack/pgpass.{DB}")
URL = f"postgresql+psycopg://channel360_test_migrator@127.0.0.1:5432/{DB}"
os.environ["IDW_MIGRATION_DATABASE_URL"] = URL
sys.path.insert(0, str(ROOT))
from sqlalchemy import create_engine, text  # noqa: E402
from packages.audit.chain import append_event  # noqa: E402

TA = UUID("bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"); TB = UUID("cccccccc-3333-4333-8333-cccccccccccc")
ID = lambda name: uuid5(NAMESPACE_URL, f"lineage-check/{DB}/{name}")  # noqa: E731
STATE = ROOT / f".runtime/iso-stack/lineage-check-{DB}.json"
EXPECT = {"O": "original", "W": "unsigned_working_copy", "W2": "unsigned_working_copy", "W3": "unsigned_working_copy", "W4": "unsigned_working_copy",
          "S": "standard", "S2": "standard", "U": "unknown", "U2": "unknown", "U3": "unknown", "OB": "original", "B1": "unknown"}
results: list[tuple[str, bool]] = []
def check(name, ok, detail=""):
    results.append((name, bool(ok))); print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
def alembic(*args):
    r = subprocess.run([str(ROOT / ".venv/bin/alembic"), *args], cwd=ROOT, capture_output=True, text=True); return r.returncode, (r.stdout + r.stderr)[-300:]
engine = create_engine(URL)
def tx(tenant):
    c = engine.connect(); t = c.begin(); c.execute(text("select set_config('app.tenant_id',:t,true)"), {"t": str(tenant)}); return c, t
def revision():
    with engine.connect() as c: return c.execute(text("select version_num from alembic_version")).scalar()

def seed():
    rc, out = alembic("downgrade", "0007_edit_set_supersession"); check("downgrade to 0007", rc == 0 and revision() == "0007_edit_set_supersession", out.strip().splitlines()[-1] if out.strip() else "")
    with engine.connect() as c:
        c.execute(text("delete from tenants where id=:t"), {"t": str(TB)}) if False else None
    c, t = tx(TB); c.execute(text("insert into tenants(id,name) values (:t,'Isolated lineage tenant B') on conflict do nothing"), {"t": str(TB)}); t.commit(); c.close()
    for tenant, doc, rows in ((TA, "DA", [("O", None, "original", 0, None), ("W", "O", "derived", 0, "wc"), ("W2", "W", "derived", 0, "rel"), ("W3", "W", "derived", 0, "rel"), ("W4", "W2", "derived", 0, None),
                                           ("S", "O", "derived", 0, "rel"), ("S2", "S", "derived", 1, None), ("U", "O", "derived", 0, None), ("U2", "U", "derived", 0, "rel"), ("U3", "U2", "derived", 0, None)]),
                              (TB, "DB", [("OB", None, "original", 0, None), ("B1", "OB", "derived", 0, "cross")])):
        c, t = tx(tenant)
        c.execute(text("insert into documents(id,tenant_id,filename,ingestion_state) values (:d,:t,:f,'staged_private')"), {"d": str(ID(doc)), "t": str(tenant), "f": f"lineage-{doc}.docx"})
        for name, parent, kind, with_edit_set, event in rows:
            f = ID("file-" + name)
            c.execute(text("insert into files(id,tenant_id,sha256,size_bytes,mime,storage_key,file_role) values (:i,:t,:s,10,'application/octet-stream',:k,:r)"),
                      {"i": str(f), "t": str(tenant), "s": ("%064x" % abs(hash(name)))[:64], "k": f"lineage-check/{name}", "r": "original" if kind == "original" else "derived"})
        c.execute(text("set constraints all deferred"))
        for name, parent, kind, with_edit_set, event in rows:
            es = None
            if with_edit_set:
                es = ID("es-" + name)
                c.execute(text("insert into edit_sets(id,tenant_id,document_id,base_version_id,created_by,reason) values (:i,:t,:d,:b,'seed','seed')"), {"i": str(es), "t": str(tenant), "d": str(ID(doc)), "b": str(ID(parent))})
            c.execute(text("""insert into document_versions(id,tenant_id,document_id,parent_version_id,file_id,kind,sha256,edit_set_id)
                              values (:i,:t,:d,:p,:f,:k,:s,:e)"""), {"i": str(ID(name)), "t": str(tenant), "d": str(ID(doc)), "p": str(ID(parent)) if parent else None, "f": str(ID("file-" + name)),
                                                                  "k": kind, "s": ("%064x" % abs(hash(name)))[:64], "e": str(es) if es else None})
        c.execute(text("update documents set current_version_id=:v where id=:d"), {"v": str(ID(rows[-1][0])), "d": str(ID(doc))})
        for state in ("scan_pending", "scanning", "scan_clean", "inspecting", "ready"):      # the guard only allows the real ingestion path
            c.execute(text("update documents set ingestion_state=:s where id=:d"), {"s": state, "d": str(ID(doc))})
        for name, parent, kind, with_edit_set, event in rows:
            if event == "wc":
                append_event(c, actor_id="seed", action="document.text_version_saved", subject=str(ID(doc)), payload={"version_id": str(ID(name)), "unsigned_working_copy": True})
            elif event == "rel":
                append_event(c, actor_id="seed", action="document.derived_version_released", subject=str(ID(doc)), payload={"version_id": str(ID(name))})
            elif event == "cross":      # tenant B event that NAMES a tenant A version, marked as a working copy: must never reclassify tenant A's U
                append_event(c, actor_id="seed", action="document.text_version_saved", subject=str(ID(doc)), payload={"version_id": str(ID("U")), "unsigned_working_copy": True})
        t.commit(); c.close()
    STATE.write_text(json.dumps({"doc_a": str(ID("DA")), "doc_b": str(ID("DB")), "versions": {k: str(ID(k)) for k in EXPECT}}))
    check("seeded historical data (10 tenant-A versions, 2 tenant-B versions)", True)

def classes():
    out = {}
    for tenant in (TA, TB):
        c, t = tx(tenant); rows = c.execute(text("select id,lineage_class from document_versions")).all(); t.rollback(); c.close()
        out.update({str(i): k for i, k in rows})
    return out

def sql():
    with engine.connect() as c:
        cols = {r[0]: (r[1], r[2], r[3]) for r in c.execute(text("select column_name,data_type,is_nullable,column_default from information_schema.columns where table_name='document_versions'"))}
        cons = [r[0] for r in c.execute(text("select pg_get_constraintdef(oid) from pg_constraint where conname='document_versions_lineage_class_check'"))]
    check("column lineage_class text NOT NULL default 'unknown'", cols.get("lineage_class", (None,))[0] == "text" and cols["lineage_class"][1] == "NO" and "unknown" in (cols["lineage_class"][2] or ""))
    check("CHECK constraint present with the four allowed values", bool(cons) and all(v in cons[0] for v in ("original", "standard", "unsigned_working_copy", "unknown")))
    got = classes(); mapping = json.loads(STATE.read_text())["versions"]
    for name, want in EXPECT.items():
        check(f"backfill {name}: {want}", got.get(mapping[name]) == want, f"got {got.get(mapping[name])}")
    try:
        c, t = tx(TA); c.execute(text("update document_versions set lineage_class='bogus' where id=:i"), {"i": mapping["S"]}); t.commit(); ok = False
    except Exception:
        ok = True
    check("constraint rejects an invalid classification", ok)
    c, t = tx(TA); n = c.execute(text("select count(*) from document_versions")).scalar(); t.rollback(); c.close()
    check("tenant isolation under RLS: tenant A sees only its 10 versions", n == 10, str(n))

def churn():
    c, t = tx(TA)
    for i in range(1100):
        append_event(c, actor_id="churn", action="document.viewed", subject=str(ID("DA")), payload={"i": i})
    n = c.execute(text("select count(*) from audit_events where action='document.viewed'")).scalar(); t.commit(); c.close()
    check("appended more than 1,000 later audit events", n >= 1100, str(n))
    got = classes(); mapping = json.loads(STATE.read_text())["versions"]
    check("classifications unchanged after the churn (they no longer depend on a recent-event page)", all(got.get(mapping[k]) == v for k, v in EXPECT.items()))

def api():
    from iso_client import Client, token  # noqa: E402
    a, b = Client(tenant=str(TA)), Client(tenant=str(TB)); state = json.loads(STATE.read_text())
    s, hist, _ = a.call("GET", f"/api/documents/{state['doc_a']}/history")
    check("tenant A history loads", s == 200, str(s))
    by_id = {v["version_id"]: v["classification"] for v in hist.get("versions", [])} if s == 200 else {}
    for name, want in EXPECT.items():
        if name in ("OB", "B1"): continue
        check(f"history label {name}: {want}", by_id.get(state["versions"][name]) == want, f"got {by_id.get(state['versions'][name])}")
    s2, _, _ = b.call("GET", f"/api/documents/{state['doc_a']}/history"); check("tenant B cannot read tenant A's history (404)", s2 == 404, str(s2))
    s3, hb, _ = b.call("GET", f"/api/documents/{state['doc_b']}/history"); cls = {v["version_id"]: v["classification"] for v in hb.get("versions", [])} if s3 == 200 else {}
    check("tenant B history: own versions original/unknown", cls.get(state["versions"]["OB"]) == "original" and cls.get(state["versions"]["B1"]) == "unknown", str(cls and list(cls.values())))
    s5, cmp_, _ = a.call("GET", f"/api/documents/{state['doc_a']}/compare?base={state['versions']['O']}&target={state['versions']['W']}")
    check("a version whose stored bytes are absent is a controlled 409, not a 500", s5 == 409 and cmp_.get("code") == "stored_file_unavailable", f"{s5} {cmp_.get('code')}")

def rollback():
    before = classes()
    rc, _ = alembic("downgrade", "0007_edit_set_supersession"); check("documented rollback: downgrade 0008 -> 0007", rc == 0 and revision() == "0007_edit_set_supersession")
    with engine.connect() as c: gone = not c.execute(text("select 1 from information_schema.columns where table_name='document_versions' and column_name='lineage_class'")).first()
    check("rollback removes the column and constraint cleanly", gone)
    rc, _ = alembic("upgrade", "head"); check("re-upgrade to head", rc == 0 and revision() == "0008_version_lineage_class")
    check("re-upgrade reproduces the exact same classifications", classes() == before)

if __name__ == "__main__":
    {"seed": seed, "migrate": lambda: check("alembic upgrade head -> 0008", alembic("upgrade", "head")[0] == 0 and revision() == "0008_version_lineage_class"), "sql": sql, "churn": churn, "api": api, "rollback": rollback}[sys.argv[1]]()
    bad = [n for n, ok in results if not ok]; print(f"{len(results) - len(bad)} passed, {len(bad)} failed"); sys.exit(1 if bad else 0)
