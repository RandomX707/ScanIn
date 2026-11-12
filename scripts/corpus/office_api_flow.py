"""Edit one real XLSX through the API (fresh tenant), then compare the saved package with the source.
Independent of the adapter: raw zip parts + lxml cell comparison + openpyxl reopen. Prints metrics only."""
import hashlib, io, json, os, random, re, subprocess, sys, urllib.parse, urllib.request, zipfile
from pathlib import Path
import openpyxl
from lxml import etree
ROOT = Path(__file__).resolve().parents[2]
API = os.environ.get("CHANNEL360_API", "http://127.0.0.1:8010")
tok = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/host/issue_test_token.py")], capture_output=True, text=True, check=True).stdout)
H = {"Authorization": f"Bearer {tok['token']}"}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

def call(method, path, body=None, headers=None):
    req = urllib.request.Request(API + path, data=body, method=method, headers={**H, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=300) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()

def cells_of(xml: bytes):
    root = etree.fromstring(xml)
    out = {}
    for c in root.iterfind(".//m:sheetData/m:row/m:c", NS):
        out[c.get("r")] = (c.get("s"), c.get("t"), etree.tostring(c.find("m:f", NS)) if c.find("m:f", NS) is not None else None,
                           (c.find("m:v", NS).text if c.find("m:v", NS) is not None else None))
    other = {tag: [etree.tostring(e) for e in root.iterfind(f"m:{tag}", NS)] for tag in ("cols", "mergeCells", "sheetFormatPr", "pageSetup", "pageMargins", "drawing", "sheetViews", "printOptions")}
    rows = {r.get("r"): (r.get("ht"), r.get("s"), r.get("customHeight")) for r in root.iterfind(".//m:sheetData/m:row", NS)}
    return out, other, rows

def compare(src: bytes, new: bytes, edited: set[str]):
    zs, zn = zipfile.ZipFile(io.BytesIO(src)), zipfile.ZipFile(io.BytesIO(new))
    res = {"parts_same_set": sorted(zs.namelist()) == sorted(zn.namelist())}
    changed = [n for n in zs.namelist() if n in zn.namelist() and zs.read(n) != zn.read(n)]
    res["changed_parts"] = changed
    problems = []
    for name in changed:
        if name.startswith("xl/worksheets/sheet"):
            a, ao, ar = cells_of(zs.read(name)); b, bo, br = cells_of(zn.read(name))
            diff = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
            if diff - edited: problems.append(f"unexpected cell changes: {sorted(diff - edited)[:5]}")
            if ao != bo: problems.append("sheet-level layout elements changed")
            if ar != br: problems.append("row heights changed")
            res["cells_changed"] = sorted(diff)
        elif name == "xl/workbook.xml":
            a, b = etree.tostring(etree.fromstring(zs.read(name)), method="c14n"), etree.tostring(etree.fromstring(zn.read(name)), method="c14n")
            ra = re.sub(rb' (fullCalcOnLoad|forceFullCalc)="1"', b"", b)
            if ra != re.sub(rb' (fullCalcOnLoad|forceFullCalc)="1"', b"", a):
                problems.append("workbook.xml changed beyond fullCalcOnLoad")
        elif name in ("[Content_Types].xml", "_rels/.rels", "docProps/custom.xml"):
            pass                                  # provenance stamp: explicit, verified permitted changes in the adapter
        elif name not in ("xl/sharedStrings.xml", "xl/calcChain.xml", "docProps/app.xml", "docProps/core.xml"):
            problems.append(f"unexpected part changed: {name}")
    wa, wb = openpyxl.load_workbook(io.BytesIO(src)), openpyxl.load_workbook(io.BytesIO(new))
    for sa, sb in zip(wa.worksheets, wb.worksheets):
        f = lambda ws: sum(1 for r in ws.iter_rows() for c in r if isinstance(c.value, str) and c.value.startswith("="))
        if (sa.title, len(sa.merged_cells.ranges), f(sa), len(sa._images)) != (sb.title, len(sb.merged_cells.ranges), f(sb), len(sb._images)):
            problems.append("sheet title/merged/formulas/images differ")
    res["problems"] = problems
    return res

def run(path: Path):
    data = path.read_bytes(); sha = hashlib.sha256(data).hexdigest()
    s, b = call("PUT", "/api/documents?filename=" + urllib.parse.quote(path.name), data, {"Content-Type": "application/octet-stream"}); assert s == 200, b[:200]
    reg = json.loads(b); d, v = reg["document_id"], reg["version_id"]; call("POST", f"/api/documents/{d}/inspect")
    s, b = call("GET", f"/api/documents/{d}/versions/{v}/preview"); pv = json.loads(b)
    if pv["edit"]["mode"] != "grid": print("  mode", pv["edit"]); return
    cells = [(c, ri, ci) for sh in pv["sheets"] for ri, row in enumerate(sh["rows"]) for ci, c in enumerate(row) if c]
    ed = [c for c, _, _ in cells if c["e"]]; locked = [c for c, _, _ in cells if not c["e"]]
    strings = [c for c in ed if c["t"] in ("string", "str", "text") and c["v"] and len(c["v"]) < 60]
    nums = [c for c in ed if c["t"] in ("decimal", "integer", "number", "int", "float")]
    rng = random.Random(3)
    picks = (rng.sample(strings, min(2, len(strings))) + rng.sample(nums, min(1, len(nums))))
    print(f"  cells {len(cells)} editable {len(ed)} locked {len(locked)} string {len(strings)} numeric {len(nums)} types {sorted({c['t'] for c in ed if c['t']})[:6]}")
    if not picks: print("  no editable cell to test"); return
    changes = []
    for c in picks:
        val = (c["v"] + " x") if c in strings else (str(int(float(c["v"] or 0)) + 1) if c["t"] in ("integer", "int") else str(float(c["v"] or 0) + 1))
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
