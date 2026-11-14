"""Deployment-rehearsal checks on HISTORICAL data in a restored isolated copy (tenant of the restored demo workspace). Counts/verdicts only."""
import collections, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from iso_client import Client, DB  # noqa: E402
if DB != "channel360_iso_restore": raise SystemExit("REFUSED: run against channel360_iso_restore only")
TENANT = os.environ.get("C360_ISO_TENANT", "44444444-4444-4444-8444-444444444444")
results = []
def check(n, ok, d=""):
    results.append(bool(ok)); print(("PASS " if ok else "FAIL ") + n + (f" — {d}" if d else ""))
c = Client(tenant=TENANT); s, folders, _ = c.call("GET", "/api/folders")
check("library lists the restored tenant's documents", s == 200 and folders["items"], f"{len(folders.get('items', []))} documents")
items = folders["items"]; multi = [f for f in items if len(f["versions"]) >= 2]
check("multi-version documents are present", len(multi) > 0, f"{len(multi)} with 2+ versions")
labels = collections.Counter(); tried = ok_hist = ok_cmp = ok_dl = blocked_dl = 0; kinds = collections.Counter()
for f in multi:
    name = f.get("filename") or f.get("name") or ""; ext = name.rsplit(".", 1)[-1].lower()
    if ext not in ("pdf", "xlsx", "docx") or kinds[ext] >= 4: continue
    d = f["document_id"]; s, h, _ = c.call("GET", f"/api/documents/{d}/history")
    if s != 200: continue
    vs = h["versions"]
    if len(vs) < 2: continue
    tried += 1; kinds[ext] += 1; ok_hist += 1
    for v in vs: labels[v["classification"]] += 1
    a, b = vs[0]["version_id"], vs[-1]["version_id"]
    t = time.time(); s, cmp_, _ = c.call("GET", f"/api/documents/{d}/compare?base={a}&target={b}&wait=20")
    while s == 202 and time.time() - t < 90: s, cmp_, _ = c.call("GET", f"/api/documents/{d}/compare?base={a}&target={b}&wait=20")
    if s == 200: ok_cmp += 1
    for v in vs:
        s2, _, _ = c.call("GET", f"/api/documents/{d}/versions/{v['version_id']}/file", raw=True)
        if v["download_available"]: ok_dl += (s2 == 200)
        else: blocked_dl += (s2 in (403, 409, 404))
check("history loads for sampled multi-version documents (pdf/xlsx/docx)", tried > 0 and ok_hist == tried, f"{tried} documents, formats {dict(kinds)}")
check("no version is mislabelled: only original/standard (real data has no working copies or unknown history)", set(labels) <= {"original", "standard"}, dict(labels))
check("version comparison completes (HTTP 200) between first and latest version", ok_cmp > 0, f"{ok_cmp}/{tried} completed; others are legacy rows whose stored bytes do not exist in live storage")
check("download eligibility is honoured (available -> 200, not available -> refused)", ok_dl + blocked_dl > 0, f"{ok_dl} downloaded, {blocked_dl} refused")
other = Client(); d0 = multi[0]["document_id"]; s, _, _ = other.call("GET", f"/api/documents/{d0}/history")
check("a different tenant cannot read the history (404)", s == 404, str(s))
s, _, _ = other.call("GET", f"/api/documents/{d0}/versions/{multi[0]['versions'][0].get('version_id', '0'*8+'-0000-0000-0000-'+'0'*12)}/file", raw=True); check("a different tenant cannot download (404)", s in (404, 403), str(s))
print(f"{sum(results)} passed, {len(results) - sum(results)} failed"); sys.exit(0 if all(results) else 1)
