"""End-to-end PDF text-edit flow through the real API in a fresh tenant. Prints metrics only (no document text)."""
import hashlib, json, os, subprocess, sys, urllib.parse, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
API = os.environ.get("CHANNEL360_API", "http://127.0.0.1:8010")
tok = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/host/issue_test_token.py")], capture_output=True, text=True, check=True).stdout)
H = {"Authorization": f"Bearer {tok['token']}"}

def call(method, path, body=None, headers=None):
    req = urllib.request.Request(API + path, data=body, method=method, headers={**H, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=300) as r: return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e: return e.code, e.read(), {}

def run(path: Path, new_text=lambda t: " ".join(t.split(" ")[:-1]) or t[:-1]):
    data = path.read_bytes(); sha = hashlib.sha256(data).hexdigest()
    s, b, _ = call("PUT", "/api/documents?filename=" + urllib.parse.quote(path.name), data, {"Content-Type": "application/octet-stream"}); assert s == 200, (s, b[:200])
    reg = json.loads(b); d, v = reg["document_id"], reg["version_id"]
    call("POST", f"/api/documents/{d}/inspect")
    s, b, _ = call("GET", f"/api/documents/{d}/versions/{v}/preview"); pv = json.loads(b)
    print("preview mode:", pv["edit"]["mode"], "| signature:", (pv.get("signature") or {}).get("status"), "| reason:", (pv["edit"].get("reason") or "")[:70])
    if pv["edit"]["mode"] != "pdf_text": return
    s, b, _ = call("GET", f"/api/documents/{d}/versions/{v}/pdf-text?page=1"); pg = json.loads(b)
    ok = [r for r in pg["runs"] if r["e"] and r["size"] >= 9 and 6 <= len(r["text"]) <= 40]
    print("page1 runs:", len(pg["runs"]), "editable:", sum(r["e"] for r in pg["runs"]), "candidates:", len(ok))
    r = ok[0]; edit = {"id": r["id"], "old": r["text"], "new": new_text(r["text"])}
    s, b, h = call("POST", f"/api/documents/{d}/versions/{v}/pdf-edit/preview", json.dumps({"pdf_edits": [edit]}).encode(), {"Content-Type": "application/json"})
    print("preview-edit:", s, h.get("X-Preview-Outcome"), "bytes", len(b), "starts", b[:5])
    s, b, _ = call("POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"pdf_edits": [edit]}).encode(), {"Content-Type": "application/json"})
    res = json.loads(b); print("save:", s, res.get("outcome"), (res.get("version") or {}).get("number"), "parent v", (res.get("version") or {}).get("parent_number"))
    nv = res["version_id"]
    s, new_bytes, _ = call("GET", f"/api/documents/{d}/versions/{nv}/file"); s2, old_bytes, _ = call("GET", f"/api/documents/{d}/versions/{v}/file")
    print("original untouched (sha):", hashlib.sha256(old_bytes).hexdigest() == sha, "| new is PDF:", new_bytes[:5] == b"%PDF-", "| new != old:", new_bytes != old_bytes)
    s, b, _ = call("GET", "/api/folders"); j = json.loads(b); f = next(x for x in (j["items"]) if x["document_id"] == d)
    print("timeline:", [(x["number"], x["parent_number"], x["outcome"]) for x in f["versions"]])
    bad = {"id": r["id"], "old": r["text"], "new": r["text"] + " ₹₹"}
    s, b, _ = call("POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"pdf_edits": [bad]}).encode(), {"Content-Type": "application/json"})
    print("refusal:", s, json.loads(b).get("code"))

if __name__ == "__main__":
    run(Path(sys.argv[1]))
