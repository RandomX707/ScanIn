#!/usr/bin/env python3
"""Independent re-open + structure check of an edited XLSX/DOCX (separate process, no application code). Prints OK or FAIL <reason>.
  verify_office_edit.py xlsx ORIGINAL EDITED SHEET!CELL[,SHEET!CELL...]
  verify_office_edit.py docx ORIGINAL EDITED "old paragraph text" "new paragraph text"
"""
import io, re, sys, zipfile

def fail(msg):
    print("FAIL " + msg); sys.exit(1)

def xlsx(orig, new, expected):
    import openpyxl
    a, b = openpyxl.load_workbook(orig), openpyxl.load_workbook(new)
    if a.sheetnames != b.sheetnames: fail("sheet names differ")
    changed = set()
    for name in a.sheetnames:
        x, y = a[name], b[name]
        if x.dimensions != y.dimensions: fail(f"{name}: dimensions {x.dimensions} -> {y.dimensions}")
        if {str(m) for m in x.merged_cells.ranges} != {str(m) for m in y.merged_cells.ranges}: fail(f"{name}: merged ranges differ")
        if len(x._images) != len(y._images): fail(f"{name}: picture count differs")
        for row in x.iter_rows():
            for c in row:
                if c.value != y[c.coordinate].value: changed.add(f"{name}!{c.coordinate}")
                if c.number_format != y[c.coordinate].number_format: fail(f"{name}!{c.coordinate}: number format changed")
        for k, d in x.column_dimensions.items():
            if d.width != y.column_dimensions[k].width: fail(f"{name}: column {k} width changed")
    if changed != set(expected): fail(f"changed cells {sorted(changed)} != expected {sorted(expected)}")
    za, zb = zipfile.ZipFile(orig), zipfile.ZipFile(new)
    if zb.testzip() is not None: fail("edited package fails zip test")
    media = [n for n in za.namelist() if "/media/" in n]
    if not all(n in zb.namelist() and za.read(n) == zb.read(n) for n in media): fail("embedded picture bytes changed")
    print("OK")

def docx(orig, new, old, newtext):
    from lxml import etree
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    za, zb = zipfile.ZipFile(orig), zipfile.ZipFile(new)
    if zb.testzip() is not None: fail("edited package fails zip test")
    protected = [n for n in za.namelist() if re.search(r"(media/|word/(styles|numbering|settings|fontTable|theme/|header|footer))", n)]
    if not protected or not all(za.read(n) == zb.read(n) for n in protected): fail("media/styles/headers changed")
    para = lambda z: ["".join(p.xpath(".//w:t/text()", namespaces={"w": W})) for p in etree.fromstring(z.read("word/document.xml")).xpath("//w:p", namespaces={"w": W})]
    pa, pb = para(za), para(zb)
    diffs = [(x, y) for x, y in zip(pa, pb) if x != y]
    if len(pa) != len(pb) or len(diffs) != 1 or diffs[0][0] != old or diffs[0][1] != newtext: fail(f"{len(diffs)} paragraph(s) differ")
    print("OK")

if __name__ == "__main__":
    kind = sys.argv[1]
    if kind == "xlsx": xlsx(sys.argv[2], sys.argv[3], [c for c in sys.argv[4].split(",") if c])
    elif kind == "docx": docx(*sys.argv[2:6])
    else: fail("unknown kind")
