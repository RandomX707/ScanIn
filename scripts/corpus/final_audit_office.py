#!/usr/bin/env python3
"""Independent audit of saved XLSX / DOCX derivatives (openpyxl, python-docx, raw zip, LibreOffice render).
Private output .private/audit/office.json; console shows check names and counts only."""
import hashlib, io, json, os, random, re, subprocess, sys, tempfile, urllib.parse, urllib.request, zipfile, glob
from pathlib import Path
import numpy as np, openpyxl, pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[2]
API = os.environ.get("CHANNEL360_API", "http://127.0.0.1:8010")
OUT = ROOT / ".private/audit"; OUT.mkdir(parents=True, exist_ok=True)
tok = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/dev/issue_iso_token.py")], capture_output=True, text=True, check=True).stdout)
H = {"Authorization": f"Bearer {tok['token']}"}
SOFFICE = "/opt/homebrew/bin/soffice"
PROFILE = tempfile.mkdtemp(prefix="lo-profile-")


def call(m, p, b=None, h=None, raw=False):
    r = urllib.request.Request(API + p, data=b, method=m, headers={**H, **(h or {})})
    try:
        with urllib.request.urlopen(r, timeout=600) as x:
            d = x.read(); return x.status, (d if raw else json.loads(d))
    except urllib.error.HTTPError as e:
        d = e.read()
        try: return e.code, json.loads(d)
        except Exception: return e.code, d


sys.path.insert(0, str(Path(__file__).resolve().parent))
from storage_read import read_version  # noqa: E402


def getbytes(d, v):
    """API download when policy allows it; otherwise (INCOMPLETE derivative, withheld by design) read isolated storage."""
    s, data = call("GET", f"/api/documents/{d}/versions/{v}/file", raw=True)
    if s == 200: return data
    assert s == 409, s
    return read_version(tok["tenant_id"], d, v)


def render(path: Path):
    outdir = Path(tempfile.mkdtemp(prefix="lo-out-"))
    subprocess.run([SOFFICE, f"-env:UserInstallation=file://{PROFILE}", "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(path)],
                   capture_output=True, timeout=180)
    pdfs = list(outdir.glob("*.pdf"))
    return pdfs[0].read_bytes() if pdfs else None


def visual(a: bytes, b: bytes, suffix: str):
    pa, pb = OUT / f"v-a{suffix}", OUT / f"v-b{suffix}"; pa.write_bytes(a); pb.write_bytes(b)
    ra, rb = render(pa), render(pb)
    if not ra or not rb: return {"rendered": False}
    da, db = pdfium.PdfDocument(ra), pdfium.PdfDocument(rb)
    tot, changed, pages_changed = 0, 0, 0
    for i in range(min(len(da), len(db))):
        A = np.asarray(da[i].render(scale=1.5).to_pil().convert("L"), dtype=np.int16); B = np.asarray(db[i].render(scale=1.5).to_pil().convert("L"), dtype=np.int16)
        if A.shape != B.shape: return {"rendered": True, "pages": (len(da), len(db)), "shape_mismatch": True}
        d = np.abs(A - B) > 10; tot += d.size; changed += int(d.sum()); pages_changed += int(d.any())
    return {"rendered": True, "pages": (len(da), len(db)), "pages_with_any_difference": pages_changed, "changed_pixel_fraction": round(changed / max(tot, 1), 6)}


def sheet_fingerprint(ws, skip: set):
    cells = {}
    for row in ws.iter_rows():
        for c in row:
            st = (c.font.name, c.font.sz, c.font.b, c.font.i, str(c.font.color.rgb if c.font.color else None), c.fill.fill_type, str(c.fill.fgColor.rgb),
                  c.border.left.style, c.border.right.style, c.border.top.style, c.border.bottom.style, c.number_format, c.alignment.horizontal, c.alignment.vertical, c.alignment.wrap_text)
            cells[c.coordinate] = (st, None if c.coordinate in skip else c.value)
    imgs = [(hashlib.sha256(i._data()).hexdigest(), i.anchor._from.col, i.anchor._from.row) for i in ws._images]
    return {"title": ws.title, "dims": ws.dimensions, "merged": sorted(str(m) for m in ws.merged_cells.ranges),
            "cols": {k: (v.width, v.hidden) for k, v in ws.column_dimensions.items()}, "rows": {k: (v.height, v.hidden) for k, v in ws.row_dimensions.items() if v.height},
            "cells": cells, "images": sorted(imgs), "freeze": str(ws.freeze_panes), "protection": ws.protection.sheet,
            "page": (ws.page_setup.orientation, ws.page_setup.scale, ws.page_setup.fitToWidth, ws.sheet_properties.pageSetUpPr.fitToPage if ws.sheet_properties.pageSetUpPr else None),
            "margins": (ws.page_margins.left, ws.page_margins.right, ws.page_margins.top, ws.page_margins.bottom), "print_area": str(ws.print_area),
            "dv": len(ws.data_validations.dataValidation) if ws.data_validations else 0, "cf": len(ws.conditional_formatting)}


def audit_xlsx(path: Path):
    data = path.read_bytes(); sha = hashlib.sha256(data).hexdigest(); res = {"file": path.name, "findings": []}
    s, reg = call("PUT", "/api/documents?filename=" + urllib.parse.quote(path.name), data, {"Content-Type": "application/octet-stream"})
    d, v = reg["document_id"], reg["version_id"]; call("POST", f"/api/documents/{d}/inspect")
    pv = call("GET", f"/api/documents/{d}/versions/{v}/preview")[1]
    cells = [c for row in pv["sheets"][0]["rows"] for c in row if c]
    rng = random.Random(9)
    strings = [c for c in cells if c["e"] and c["t"] == "string" and c["v"] and len(c["v"]) < 60]
    blanks = [c for c in cells if c["e"] and c["t"] == "null"]
    nums = [c for c in cells if c["e"] and c["t"] in ("integer", "decimal")]
    picks = rng.sample(strings, 1) + rng.sample(blanks, 2) + (rng.sample(nums, 1) if nums else [])
    changes, expect = [], {}
    for i, c in enumerate(picks):
        ref = c["id"].split("!")[-1]
        if c["t"] == "string": val, exp = c["v"] + " x", c["v"] + " x"
        elif c["t"] == "null": val, exp = (["Acme Test Ltd", "12345"][i % 2], None); exp = "Acme Test Ltd" if i % 2 == 1 or True else None
        elif c["t"] == "integer": val, exp = str(int(float(c["v"])) + 1), int(float(c["v"])) + 1
        else: val, exp = str(float(c["v"]) + 1), float(c["v"]) + 1
        changes.append({"target_id": c["id"], "fingerprint": c["fp"], "value": val}); expect[ref] = (val, c["t"])
    s, b = call("POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"changes": changes}).encode(), {"Content-Type": "application/json"})
    if s != 200:
        res["findings"].append(f"save {s} {(b or {}).get('code')}"); return res
    new = getbytes(d, b["version_id"])
    res["outcome"] = b["outcome"]; res["note"] = b.get("note")
    skip = set(expect)
    wa, wb = openpyxl.load_workbook(io.BytesIO(data)), openpyxl.load_workbook(io.BytesIO(new))
    if wa.sheetnames != wb.sheetnames: res["findings"].append("sheet names differ")
    for sa, sb in zip(wa.worksheets, wb.worksheets):
        fa, fb = sheet_fingerprint(sa, skip), sheet_fingerprint(sb, skip)
        for k in fa:
            if fa[k] != fb[k]: res["findings"].append(f"{k} differs")
    ws = wb.worksheets[0]
    for ref, (val, t) in expect.items():
        got = ws[ref].value
        ok = str(got) == val or (t == "null" and str(got) in (val, str(float(val)) if re.fullmatch(r"-?\d+\.?\d*", val) else val)) or (str(got).rstrip("0").rstrip(".") == val.rstrip("0").rstrip("."))
        if not ok: res["findings"].append(f"edited cell {ref} reads {got!r} expected {val!r}")
    formulas_a = {c.coordinate: c.value for r in wa.worksheets[0].iter_rows() for c in r if isinstance(c.value, str) and c.value.startswith("=")}
    formulas_b = {c.coordinate: c.value for r in wb.worksheets[0].iter_rows() for c in r if isinstance(c.value, str) and c.value.startswith("=")}
    res["formulas"] = len(formulas_a)
    if formulas_a != formulas_b: res["findings"].append("formula strings differ")
    if wa.defined_names.keys() != wb.defined_names.keys() if hasattr(wa.defined_names, "keys") else False: res["findings"].append("defined names differ")
    # formula cache: recalculation limitation (values cached by Excel are NOT recomputed by Channel360)
    ca = openpyxl.load_workbook(io.BytesIO(data), data_only=True).worksheets[0]; cb = openpyxl.load_workbook(io.BytesIO(new), data_only=True).worksheets[0]
    res["formula_cached_values_unchanged_stale_until_excel_opens"] = all(ca[k].value == cb[k].value for k in formulas_a)
    z = zipfile.ZipFile(io.BytesIO(new)); wbx = z.read("xl/workbook.xml").decode()
    res["fullCalcOnLoad_set"] = "fullCalcOnLoad" in wbx
    res["original_sha_unchanged"] = hashlib.sha256(call("GET", f"/api/documents/{d}/versions/{v}/file", raw=True)[1]).hexdigest() == sha
    if not res["original_sha_unchanged"]: res["findings"].append("original changed")
    res["visual_libreoffice"] = visual(data, new, ".xlsx")
    return res


def audit_docx(path: Path):
    import docx
    data = path.read_bytes(); sha = hashlib.sha256(data).hexdigest(); res = {"file": path.name, "findings": []}
    s, reg = call("PUT", "/api/documents?filename=" + urllib.parse.quote(path.name), data, {"Content-Type": "application/octet-stream"})
    d, v = reg["document_id"], reg["version_id"]; call("POST", f"/api/documents/{d}/inspect")
    blocks = call("GET", f"/api/documents/{d}/versions/{v}/preview")[1]["blocks"]
    ed = [x for x in blocks if x["e"] and len(x["text"]) > 12]
    # one paragraph and one table cell
    para = next(x for x in ed if not x["table"]); cell = next((x for x in ed if x["table"]), None)
    chs = [{"target_id": x["target_id"], "fingerprint": x["fp"], "value": x["text"] + " e2e"} for x in (para, cell) if x]
    s, b = call("POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"changes": chs}).encode(), {"Content-Type": "application/json"})
    if s != 200:
        res["findings"].append(f"save {s} {(b or {}).get('code')}"); return res
    new = getbytes(d, b["version_id"])
    res["outcome"] = b["outcome"]; res["note"] = b.get("note")
    za, zn = zipfile.ZipFile(io.BytesIO(data)), zipfile.ZipFile(io.BytesIO(new))
    res["parts_added"] = sorted(set(zn.namelist()) - set(za.namelist())); res["parts_removed"] = sorted(set(za.namelist()) - set(zn.namelist()))
    changed = sorted(n for n in za.namelist() if n in zn.namelist() and za.read(n) != zn.read(n))
    res["changed_parts"] = changed
    allowed = {"word/document.xml", "[Content_Types].xml", "_rels/.rels", "docProps/custom.xml"}
    if set(changed) - allowed or set(res["parts_added"]) - allowed: res["findings"].append(f"unexpected parts {sorted((set(changed) | set(res['parts_added'])) - allowed)}")
    for part in ("word/styles.xml", "word/settings.xml", "word/numbering.xml", "word/_rels/document.xml.rels", "word/theme/theme1.xml", "word/fontTable.xml"):
        if part in za.namelist() and za.read(part) != zn.read(part): res["findings"].append(f"{part} differs")
    for n in za.namelist():
        if "media/" in n and za.read(n) != zn.read(n): res["findings"].append(f"media {n} differs")
    x = lambda z: z.read("word/document.xml").decode()
    xa, xn = x(za), x(zn)
    cnt = lambda s_, pat: len(re.findall(pat, s_))
    for name, pat in (("tables", r"<w:tbl>"), ("rows", r"<w:tr[ >]"), ("drawings", r"<w:drawing>"), ("hyperlinks", r"<w:hyperlink\b"), ("paragraphs", r"<w:p[ >]"), ("ins", r"<w:ins\b"), ("del", r"<w:del\b"), ("sectPr", r"<w:sectPr"), ("bookmarks", r"<w:bookmarkStart")):
        if cnt(xa, pat) != cnt(xn, pat): res["findings"].append(f"{name} count {cnt(xa, pat)}->{cnt(xn, pat)}")
    ra = re.findall(r'Target="([^"]+)"', za.read("word/_rels/document.xml.rels").decode()); rn = re.findall(r'Target="([^"]+)"', zn.read("word/_rels/document.xml.rels").decode())
    res["relationship_targets_identical"] = ra == rn; res["external_links"] = len([t for t in ra if t.startswith("http")])
    res["trackRevisions_setting_before_after"] = (b"trackRevisions" in za.read("word/settings.xml"), b"trackRevisions" in zn.read("word/settings.xml"))
    da, dn = docx.Document(io.BytesIO(data)), docx.Document(io.BytesIO(new))
    ta = [p.text for p in da.paragraphs] + [c.text for t in da.tables for r in t.rows for c in r.cells]
    tn = [p.text for p in dn.paragraphs] + [c.text for t in dn.tables for r in t.rows for c in r.cells]
    diffs = [i for i, (p, q) in enumerate(zip(ta, tn)) if p != q]
    res["text_blocks_changed"] = len(diffs); res["text_blocks_expected"] = len(chs)
    if len(ta) != len(tn) or len(diffs) > len(chs) + 2: res["findings"].append("more text changed than edited")
    res["styles_used_identical"] = [p.style.name for p in da.paragraphs] == [p.style.name for p in dn.paragraphs]
    if not res["styles_used_identical"]: res["findings"].append("paragraph styles changed")
    res["original_sha_unchanged"] = hashlib.sha256(call("GET", f"/api/documents/{d}/versions/{v}/file", raw=True)[1]).hexdigest() == sha
    res["visual_libreoffice"] = visual(data, new, ".docx")
    return res


if __name__ == "__main__":
    out = []
    files = sorted(set(glob.glob(str(ROOT / ".private/corpus/QCR/QCR/*")) + glob.glob(str(ROOT / ".private/corpus/RFQ/RFQ/*"))))
    for f in files:
        f = Path(f)
        if f.suffix.lower() == ".xlsx": r = audit_xlsx(f)
        elif f.suffix.lower() == ".docx": r = audit_docx(f)
        else: continue
        out.append(r)
        extra = {k: r[k] for k in ("outcome", "formulas", "fullCalcOnLoad_set", "formula_cached_values_unchanged_stale_until_excel_opens", "visual_libreoffice", "text_blocks_changed", "trackRevisions_setting_before_after", "relationship_targets_identical") if k in r}
        print(f.name[:22].ljust(22), "FINDINGS:", r["findings"] or "none", "|", json.dumps(extra)[:260])
    json.dump(out, open(OUT / "office.json", "w"), indent=1, default=str)
