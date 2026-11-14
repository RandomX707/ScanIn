"""Read-only inventory of the private stakeholder corpus.

Input : ./Policy Copies/ (gitignored).  Output: ./.private/inventory/ (gitignored).
Nothing from the corpus is printed beyond counts/types; all detail goes to the private JSON.
"""
import hashlib, json, re, sys, zipfile
from pathlib import Path
import openpyxl, pypdf, pypdfium2
from docx import Document

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "Policy Copies"
OUT = ROOT / ".private" / "inventory"
EXTRACT = ROOT / ".private" / "corpus"
OUT.mkdir(parents=True, exist_ok=True)

def sha(b): return hashlib.sha256(b).hexdigest()

def pdf_info(data: bytes):
    d = {"kind": "pdf"}
    try:
        r = pypdf.PdfReader(__import__("io").BytesIO(data))
        d["encrypted"] = r.is_encrypted
        d["pages"] = len(r.pages)
        fields = r.get_fields() or {}
        d["acroform_fields"] = len(fields)
        d["field_types"] = sorted({str(v.get("/FT")) for v in fields.values()})
    except Exception as e:
        d["pypdf_error"] = repr(e); return d
    pdf = pypdfium2.PdfDocument(data)
    tchars, empty, imgs = 0, [], 0
    for i in range(len(pdf)):
        pg = pdf[i]; tp = pg.get_textpage(); n = tp.count_chars(); tchars += n
        if n < 20: empty.append(i + 1)
        imgs += sum(1 for o in pg.get_objects() if o.type == 3)
    d["text_chars"] = tchars; d["pages_without_text"] = empty; d["image_objects"] = imgs
    raw = data
    br = [m.group(0).decode("latin1") for m in re.finditer(rb"/ByteRange\s*\[[^\]]*\]", raw)]
    d["byterange_markers"] = len(br)
    d["byterange_raw"] = br
    d["sig_markers"] = {k: len(re.findall(k.encode(), raw)) for k in ("/Type\\s*/Sig", "/SubFilter", "/Contents\\s*<", "/FT\\s*/Sig", "/Encrypt", "/DocMDP", "/Perms")}
    ranges = []
    for s in br:
        nums = [int(x) for x in re.findall(r"\d+", s)]
        if len(nums) >= 4:
            a, b, c, e = nums[:4]
            ranges.append({"covers_to": c + e, "file_size": len(raw), "in_bounds": c + e <= len(raw), "covers_whole_file": c + e == len(raw)})
    d["byterange_analysis"] = ranges
    d["fonts"] = sorted({str(f.get("/BaseFont")) for p in r.pages for f in [x.get_object() for x in (p.get("/Resources", {}).get("/Font", {}) or {}).values()]})[:40] if True else []
    return d

def xlsx_info(data: bytes):
    import io
    d = {"kind": "xlsx"}
    z = zipfile.ZipFile(io.BytesIO(data))
    d["parts"] = len(z.namelist()); d["media"] = [n for n in z.namelist() if "/media/" in n]
    d["has_vba"] = any("vbaProject" in n for n in z.namelist())
    wb = openpyxl.load_workbook(io.BytesIO(data))
    d["sheets"] = []
    for ws in wb.worksheets:
        formulas = sum(1 for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("="))
        vals = sum(1 for row in ws.iter_rows() for c in row if c.value not in (None, ""))
        d["sheets"].append({"name": ws.title, "dims": ws.dimensions, "rows": ws.max_row, "cols": ws.max_column,
                            "formulas": formulas, "non_empty_cells": vals, "merged": len(ws.merged_cells.ranges),
                            "images": len(ws._images), "protected": bool(ws.protection.sheet),
                            "data_validations": len(ws.data_validations.dataValidation) if ws.data_validations else 0,
                            "conditional_formats": len(ws.conditional_formatting)})
    d["formulas"] = sum(s["formulas"] for s in d["sheets"]); d["merged"] = sum(s["merged"] for s in d["sheets"])
    return d

def docx_info(data: bytes):
    import io
    d = {"kind": "docx"}
    z = zipfile.ZipFile(io.BytesIO(data)); xml = z.read("word/document.xml").decode("utf8", "replace")
    doc = Document(io.BytesIO(data))
    d["paragraphs"] = len(doc.paragraphs); d["tables"] = len(doc.tables)
    d["media"] = [n for n in z.namelist() if "/media/" in n]
    d["inline_or_anchor_images"] = len(re.findall(r"<wp:(inline|anchor)\b", xml))
    d["external_hyperlinks"] = len(re.findall(r'TargetMode="External"', z.read("word/_rels/document.xml.rels").decode()))
    d["tracked_ins"] = len(re.findall(r"<w:ins\b", xml)); d["tracked_del"] = len(re.findall(r"<w:del\b", xml))
    d["trackRevisions_setting"] = b"trackRevisions" in z.read("word/settings.xml") if "word/settings.xml" in z.namelist() else False
    d["comments"] = "word/comments.xml" in z.namelist()
    d["headers_footers"] = [n for n in z.namelist() if re.match(r"word/(header|footer)\d*\.xml", n)]
    d["document_protection"] = b"documentProtection" in z.read("word/settings.xml")
    return d

def describe(name: str, data: bytes):
    ext = name.rsplit(".", 1)[-1].lower()
    base = {"bytes": len(data), "sha256": sha(data)}
    return {**base, **({"pdf": pdf_info, "xlsx": xlsx_info, "docx": docx_info}.get(ext, lambda b: {"kind": ext})(data))}

occ, seen = [], {}
for f in sorted(SRC.iterdir()):
    if f.suffix.lower() == ".pdf":
        occ.append({"source": "standalone", "path": f.name, **describe(f.name, f.read_bytes())})
for zf in sorted(SRC.glob("*.zip")):
    z = zipfile.ZipFile(zf)
    for i in z.infolist():
        if i.is_dir(): continue
        data = z.read(i)
        mode = (i.external_attr >> 16) & 0o170000
        occ.append({"source": zf.name, "path": i.filename, "zip_encrypted": bool(i.flag_bits & 1), "zip_symlink": mode == 0o120000,
                    "compress_ratio": round(i.file_size / max(i.compress_size, 1), 2), **describe(i.filename, data)})
        dest = EXTRACT / zf.stem / i.filename
        dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(data)
for o in occ: seen.setdefault(o["sha256"], []).append(f'{o["source"]}:{o["path"]}')
unique = {h: v for h, v in seen.items()}
(OUT / "manifest.json").write_text(json.dumps({"occurrences": occ, "unique": unique}, indent=1, ensure_ascii=False))
kinds = {}
for h, v in unique.items():
    k = next(o["kind"] for o in occ if o["sha256"] == h); kinds[k] = kinds.get(k, 0) + 1
print("occurrences:", len(occ), "unique:", len(unique), kinds)
print("by source:", {s: sum(1 for o in occ if o["source"] == s) for s in {o["source"] for o in occ}})
print("pdf pages (unique):", sum(next(o for o in occ if o["sha256"] == h).get("pages", 0) for h in unique if next(o for o in occ if o["sha256"] == h)["kind"] == "pdf"))
print("formulas (unique xlsx):", sum(next(o for o in occ if o["sha256"] == h).get("formulas", 0) for h in unique if next(o for o in occ if o["sha256"] == h)["kind"] == "xlsx"),
      "merged:", sum(next(o for o in occ if o["sha256"] == h).get("merged", 0) for h in unique if next(o for o in occ if o["sha256"] == h)["kind"] == "xlsx"))
