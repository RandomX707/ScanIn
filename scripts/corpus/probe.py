"""Drive the running local API with every unique private corpus file in a FRESH isolated tenant.
Output (private, gitignored): .private/inventory/probe-<label>.json ; stdout shows only per-file outcome codes."""
import hashlib, json, subprocess, sys, urllib.request, urllib.parse
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
API = "http://127.0.0.1:8000"
label = sys.argv[1] if len(sys.argv) > 1 else "baseline"
tok = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/host/issue_test_token.py")], capture_output=True, text=True, check=True).stdout)
H = {"Authorization": f"Bearer {tok['token']}"}

def call(method, path, body=None, headers=None):
    req = urllib.request.Request(API + path, data=body, method=method, headers={**H, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=300) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()

m = json.load(open(ROOT / ".private/inventory/manifest.json"))
files, seen = [], set()
for o in m["occurrences"]:
    if o["sha256"] in seen: continue
    seen.add(o["sha256"])
    p = ROOT / "Policy Copies" / o["path"] if o["source"] == "standalone" else ROOT / ".private/corpus" / o["source"].removesuffix(".zip") / o["path"]
    files.append((o, p))
res = []
for o, p in files:
    data = p.read_bytes(); name = p.name; r = {"path": o["path"], "kind": o["kind"], "sha256": o["sha256"]}
    s, b = call("PUT", "/api/documents?filename=" + urllib.parse.quote(name), data, {"Content-Type": "application/octet-stream"})
    r["upload"] = s
    if s >= 300: r["upload_body"] = b[:300].decode("utf8", "replace"); res.append(r); print("UPLOAD-FAIL", s, o["path"]); continue
    reg = json.loads(b); did, vid = reg["document_id"], reg["version_id"]
    s, b = call("POST", f"/api/documents/{did}/inspect"); r["inspect"] = s
    try: r["inspect_state"] = json.loads(b).get("state")
    except Exception: r["inspect_state"] = None
    s, b = call("GET", f"/api/documents/{did}/versions/{vid}/file"); r["download_status"] = s; r["download_sha_matches"] = hashlib.sha256(b).hexdigest() == o["sha256"]
    s, b = call("GET", f"/api/documents/{did}/versions/{vid}/preview"); r["preview_status"] = s
    if s == 200:
        pj = json.loads(b); r["edit"] = pj.get("edit"); r["preview_kind"] = pj.get("kind")
    s, b = call("GET", f"/api/documents/{did}/versions/{vid}/chunks"); r["chunks_status"] = s
    if s == 200:
        cj = json.loads(b); ch = cj.get("chunks") or cj.get("sections") or []
        r["chunk_count"] = len(ch); r["chunk_keys"] = list(cj.keys())[:12]
    res.append(r)
    print(f"{o['path'][:34]:34} up={r['upload']} insp={r.get('inspect_state')} dl={'OK' if r.get('download_sha_matches') else 'BAD'} edit={(r.get('edit') or {}).get('mode')} chunks={r.get('chunk_count')}")
(ROOT / f".private/inventory/probe-{label}.json").write_text(json.dumps({"tenant": tok["tenant_id"], "results": res}, indent=1))
