"""Import real archives through the API in ONE fresh tenant and report per-entry outcomes (names/statuses only)."""
import json, os, subprocess, sys, urllib.parse, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
API = os.environ.get("CHANNEL360_API", "http://127.0.0.1:8010")
tok = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/host/issue_test_token.py")], capture_output=True, text=True, check=True).stdout)
H = {"Authorization": f"Bearer {tok['token']}"}

def call(method, path, body=None, headers=None):
    req = urllib.request.Request(API + path, data=body, method=method, headers={**H, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=900) as r: return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read())
        except Exception: return e.code, None

def upload_zip(path: Path, duplicates="link"):
    s, j = call("PUT", f"/api/archives?filename={urllib.parse.quote(path.name)}&duplicates={duplicates}", path.read_bytes(), {"Content-Type": "application/octet-stream"})
    return s, j

if __name__ == "__main__":
    for name in sys.argv[1:]:
        s, j = upload_zip(Path(name))
        print(Path(name).name, "HTTP", s)
        if s == 200:
            print("  summary:", j["summary"])
            for e in j["entries"]: print(f"   {e['status']:16} {e['path']}" + (f"  [{e['reason']}]" if e["reason"] else ""))
        else: print("  ", j)
    s, j = call("GET", "/api/folders")
    items = j["items"]; print("folders in tenant:", len(items), "with archive source:", sum(1 for f in items if f.get("source")))
    print("sample sources:", [(f["name"], f["source"]) for f in items[:3]])
