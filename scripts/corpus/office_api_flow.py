"""Edit one real XLSX through the API (fresh tenant), then compare the saved package with the source.
Independent of the adapter: raw zip parts + lxml cell comparison + openpyxl reopen. Prints metrics only."""
import hashlib, io, json, os, random, re, subprocess, sys, urllib.parse, urllib.request, zipfile
from pathlib import Path
import openpyxl
from lxml import etree
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
API = os.environ.get("CHANNEL360_API", "http://127.0.0.1:8010")
tok = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/host/issue_test_token.py")], capture_output=True, text=True, check=True).stdout)
H = {"Authorization": f"Bearer {tok['token']}"}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

def call(method, path, body=None, headers=None):
    req = urllib.request.Request(API + path, data=body, method=method, headers={**H, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=300) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()

from ooxml_compare import cells_of, compare  # noqa: E402

def run(path: Path):
    data = path.read_bytes(); sha = hashlib.sha256(data).hexdigest()
    s, b = call("PUT", "/api/documents?filename=" + urllib.parse.quote(path.name), data, {"Content-Type": "application/octet-stream"}); assert s == 200, b[:200]
    reg = json.loads(b); d, v = reg["document_id"], reg["version_id"]; call("POST", f"/api/documents/{d}/inspect")
    s, b = call("GET", f"/api/documents/{d}/versions/{v}/preview"); pv = json.loads(b)
    if pv["edit"]["mode"] != "grid": print("  mode", pv["edit"]); return
    cells = [(c, ri, ci) for sh in pv["sheets"] for ri, row in enumerate(sh["rows"]) for ci, c in enumerate(row) if c]
    ed = [c for c, _, _ in cells if c["e"]]; locked = [c for c, _, _ in cells if not c["e"]]
    blanks = [c for c in ed if c["t"] == "null"]
    strings = [c for c in ed if c["t"] in ("string", "str", "text") and c["v"] and len(c["v"]) < 60]
    nums = [c for c in ed if c["t"] in ("decimal", "integer", "number", "int", "float")]
    rng = random.Random(3)
    picks = (rng.sample(strings, min(2, len(strings))) + rng.sample(nums, min(1, len(nums))))
    if os.environ.get("FILL_BLANKS"):
        picks = rng.sample(blanks, min(3, len(blanks)))
        print(f"  blank editable cells: {len(blanks)}")
    print(f"  cells {len(cells)} editable {len(ed)} locked {len(locked)} string {len(strings)} numeric {len(nums)} types {sorted({c['t'] for c in ed if c['t']})[:6]}")
    if not picks: print("  no editable cell to test"); return
    changes = []
    for c in picks:
        val = (["Acme Test Ltd", "12345", "7.5"][picks.index(c) % 3]) if c in blanks else (c["v"] + " x") if c in strings else (str(int(float(c["v"] or 0)) + 1) if c["t"] in ("integer", "int") else str(float(c["v"] or 0) + 1))
        changes.append({"target_id": c["id"], "fingerprint": c["fp"], "value": val})
    s, b = call("POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"changes": changes}).encode(), {"Content-Type": "application/json"})
    res = json.loads(b)
    if s != 200: print("  SAVE", s, res.get("code"), (res.get("message") or "")[:100]); return
    nv = res["version_id"]
    s1, new = call("GET", f"/api/documents/{d}/versions/{nv}/file"); s2, old = call("GET", f"/api/documents/{d}/versions/{v}/file")
    ids = {c["id"] for c in picks}
    edited_refs = {i.split("!")[-1].split(":")[-1].replace("$", "") for i in ids}
    cmp = compare(data, new, edited_refs)
    print(f"  saved outcome={res.get('outcome')} note={res.get('note')} v{(res.get('version') or {}).get('number')} orig_sha_ok={hashlib.sha256(old).hexdigest() == sha}")
    print(f"  parts_same_set={cmp['parts_same_set']} changed_parts={cmp['changed_parts']} cells_changed={cmp.get('cells_changed')} problems={cmp['problems']}")

if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(Path(p).name); run(Path(p))
