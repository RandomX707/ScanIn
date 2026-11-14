"""Independent OOXML comparison helpers (raw zip parts + lxml + openpyxl); no dependency on the adapters under test."""
import io, re, zipfile
import openpyxl
from lxml import etree

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


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

