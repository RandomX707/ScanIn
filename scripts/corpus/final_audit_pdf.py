#!/usr/bin/env python3
"""Independent audit of saved PDF derivatives (qpdf + pypdf + pdfium only; never the editing engine).
Private output: .private/audit/pdf.json. Console: check names and counts only (no document text)."""
import hashlib, json, os, re, subprocess, sys, urllib.parse, urllib.request
from pathlib import Path
import numpy as np, pypdf, pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
API = os.environ.get("CHANNEL360_API", "http://127.0.0.1:8010")
OUT = ROOT / ".private/audit"; OUT.mkdir(parents=True, exist_ok=True)
STD = re.compile(r"^(Helvetica|Times|Courier)")
tok = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/dev/issue_iso_token.py")], capture_output=True, text=True, check=True).stdout)
H = {"Authorization": f"Bearer {tok['token']}"}


def call(m, p, b=None, h=None, raw=False):
    r = urllib.request.Request(API + p, data=b, method=m, headers={**H, **(h or {})})
    try:
        with urllib.request.urlopen(r, timeout=600) as x:
            d = x.read(); return x.status, (d if raw else json.loads(d))
    except urllib.error.HTTPError as e:
        d = e.read()
        try: return e.code, json.loads(d)
        except Exception: return e.code, d


def fonts_of(data: bytes) -> set:
    rd = pypdf.PdfReader(__import__("io").BytesIO(data), strict=False); out = set(); seen = set()
    def walk(res, depth=0):
        if res is None or depth > 6: return
        res = res.get_object()
        for f in (res.get("/Font") or {}).values():
            out.add(str(f.get_object().get("/BaseFont", "")))
        for x in (res.get("/XObject") or {}).values():
            x = x.get_object()
            if str(x.get("/Subtype")) == "/Form" and id(x) not in seen:
                seen.add(id(x)); walk(x.get("/Resources"), depth + 1)
    for pg in rd.pages:
        r = pg
        for _ in range(8):
            if "/Resources" in r: walk(r["/Resources"]); break
            r = r.get("/Parent"); r = r.get_object() if r is not None else None
            if r is None: break
    return out


def struct(data: bytes):
    rd = pypdf.PdfReader(__import__("io").BytesIO(data), strict=False); d = pdfium.PdfDocument(data)
    return {"pages": len(rd.pages), "sizes": [(round(float(p.mediabox.width), 1), round(float(p.mediabox.height), 1)) for p in rd.pages],
            "images": sum(1 for i in range(len(d)) for o in d[i].get_objects() if o.type == 3),
            "annots": sum(len(p.get("/Annots") or []) for p in rd.pages), "fields": len(rd.get_fields() or {}), "outline": len(rd.outline)}


def norm(s): return re.sub(r"\s+", "", s)
def toks(s): return set(re.findall(r"[A-Za-z0-9]{3,}", s))


def qpdf_ok(data: bytes) -> tuple[bool, str]:
    p = OUT / "q.pdf"; p.write_bytes(data)
    r = subprocess.run(["qpdf", "--check", str(p)], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip().splitlines()[-1][:80] if (r.stderr or r.stdout) else ""


def audit_pair(src: bytes, out: bytes, edits: list[dict], res: dict, label: str):
    """src = parent bytes, out = derivative bytes, edits = [{page(1-based), old, new, bbox}]"""
    c = res.setdefault(label, {})
    ok, msg = qpdf_ok(out); c["qpdf_check"] = ok
    try:
        pypdf.PdfReader(__import__("io").BytesIO(out), strict=True); c["pypdf_strict_reopen"] = True
    except Exception as e: c["pypdf_strict_reopen"] = f"{type(e).__name__}"
    a, b = struct(src), struct(out)
    c["structure_preserved"] = (a == b); c["structure_diff"] = {k: (a[k], b[k]) for k in a if a[k] != b[k]}
    fa, fb = fonts_of(src), fonts_of(out)
    added = fb - fa
    c["fonts_original_kept"] = fa <= fb; c["fonts_added"] = sorted(added); c["fonts_added_only_standard_substitutes"] = all(STD.match(x.lstrip("/")) for x in added)
    da, db = pdfium.PdfDocument(src), pdfium.PdfDocument(out)
    edited_pages = {e["page"] - 1 for e in edits}
    text_ok, stray, overlap, clipped, found_all, residue = True, 0, 0, 0, True, 0
    for i in range(len(da)):
        ta, tb = da[i].get_textpage().get_text_range(), db[i].get_textpage().get_text_range()
        ia, ib = da[i].render(scale=2.0).to_pil().convert("L"), db[i].render(scale=2.0).to_pil().convert("L")
        assert ia.size == ib.size                       # identical resolution
        A, B = np.asarray(ia, dtype=np.int16), np.asarray(ib, dtype=np.int16)
        diff = np.abs(A - B) > 10
        if i not in edited_pages:
            text_ok &= (ta == tb); stray += int(diff.sum()); continue
        H_ = float(pypdf.PdfReader(__import__("io").BytesIO(src)).pages[i].mediabox.height)
        allowed = np.zeros_like(diff)
        tp = db[i].get_textpage()
        for e in [x for x in edits if x["page"] - 1 == i]:
            x0, y0, x1, y1 = e["bbox"]
            # new text rectangle located independently by pdfium text search
            rect = None
            srch = tp.search(e["new"].strip(), match_case=True) if e["new"].strip() else None
            m = srch.get_next() if srch else None
            if m is None and e["new"].strip():
                srch = tp.search(e["new"].strip().split()[0], match_case=True); m = srch.get_next()
            if m is None:
                found_all = False
            else:
                l = min(tp.get_charbox(k)[0] for k in range(m[0], m[0] + m[1])); r = max(tp.get_charbox(k)[2] for k in range(m[0], m[0] + m[1]))
                bt = min(tp.get_charbox(k)[1] for k in range(m[0], m[0] + m[1])); tt = max(tp.get_charbox(k)[3] for k in range(m[0], m[0] + m[1]))
                rect = (l, bt, r, tt); x0, x1 = min(x0, l), max(x1, r)
                pw, ph = db[i].get_size()
                if l < -1 or r > pw + 1 or bt < -1 or tt > ph + 1: clipped += 1
                # overlap: other characters whose centre lies inside the new rectangle
                mine = set(range(m[0], m[0] + m[1]))
                for k in range(tp.count_chars()):
                    if k in mine: continue
                    cl, cb, cr, ct = tp.get_charbox(k)
                    if (cl + cr) / 2 > l + 0.5 and (cl + cr) / 2 < r - 0.5 and (cb + ct) / 2 > bt + 0.5 and (cb + ct) / 2 < tt - 0.5 and tp.get_text_range(k, 1).strip():
                        overlap += 1
            allowed[max(int((H_ - y1 - 3) * 2), 0):int((H_ - y0 + 3) * 2) + 1, max(int((x0 - 3) * 2), 0):int((x1 + 3) * 2) + 1] = True
            # original wording gone (per line) unless it legitimately remains / is contained in the new text
            lines_b = [norm(l) for l in re.split(r"[\r\n]+", tb) if l.strip()]
            if norm(e["old"]) not in norm(e["new"]) and any(norm(e["old"]) in l for l in lines_b) and sum(norm(e["old"]) in norm(l) for l in re.split(r"[\r\n]+", ta)) < 2:
                residue += 1
        stray += int((diff & ~allowed).sum())
        # text integrity: tokens removed/added must come only from the edits
        gone, new = toks(ta) - toks(tb), toks(tb) - toks(ta)
        allowed_old = set().union(*[toks(e["old"]) for e in edits if e["page"] - 1 == i]); allowed_new = set().union(*[toks(e["new"]) for e in edits if e["page"] - 1 == i])
        text_ok &= (gone <= allowed_old and new <= allowed_new)
    c["unedited_and_edited_page_text_integrity"] = bool(text_ok)
    c["pixels_outside_permitted_regions"] = stray
    c["new_text_found_by_pdfium_search"] = found_all
    c["old_wording_residue"] = residue
    c["clipped_outside_page"] = clipped
    c["overlapping_foreign_characters"] = overlap
    return c


def run(path: Path):
    data = path.read_bytes(); sha = hashlib.sha256(data).hexdigest(); res = {"file": path.name, "checks": {}}
    s, reg = call("PUT", "/api/documents?filename=" + urllib.parse.quote(path.name), data, {"Content-Type": "application/octet-stream"})
    d, v1 = reg["document_id"], reg["version_id"]; call("POST", f"/api/documents/{d}/inspect")
    get = lambda v: call("GET", f"/api/documents/{d}/versions/{v}/file", raw=True)[1]
    runs_of = lambda v, p: call("GET", f"/api/documents/{d}/versions/{v}/pdf-text?page={p}")[1]["runs"]
    def pick(v, kind):
        for page in (1, 2, 3):
            rs = [r for r in runs_of(v, page) if r["e"] and 5 <= len(r["text"].strip()) <= 40]
            if kind == "table_value": rs = [r for r in rs if any(ch.isdigit() for ch in r["text"]) and len(r["text"]) <= 20]
            if not rs: continue
            if kind == "heading": c = max(rs, key=lambda r: (r["size"], len(r["text"])))
            elif kind == "table_value": c = next((r for r in rs if any(ch.isdigit() for ch in r["text"]) and len(r["text"]) <= 20), rs[-1])
            else: c = next((r for r in rs if " " in r["text"].strip() and r["size"] < 11), rs[len(rs) // 2])
            return page, c
    chain, parent = [], v1
    for kind in ("heading", "text_span", "table_value"):
        page, r = pick(parent, kind)
        if kind == "table_value": new = "".join(str((int(ch) + 1) % 10) if ch.isdigit() else ch for ch in r["text"])
        elif kind == "heading": new = r["text"].rstrip()[:-1] if len(r["text"].rstrip()) > 6 else r["text"]
        else: new = " ".join(r["text"].split(" ")[:-1]) or r["text"][:-1]
        s, b = call("POST", f"/api/documents/{d}/versions/{parent}/save", json.dumps({"pdf_edits": [{"id": r["id"], "old": r["text"], "new": new}]}).encode(), {"Content-Type": "application/json"})
        label = f"chain_{kind}"
        if s != 200:
            res["checks"][label] = {"save": f"{s} {(b or {}).get('code')}"}; continue
        out = get(b["version_id"])
        audit_pair(get(parent), out, [{"page": page, "old": r["text"], "new": new, "bbox": r["bbox"]}], res["checks"], label)
        res["checks"][label]["save"] = "200 " + b["outcome"]; res["checks"][label]["kind"] = kind
        parent = b["version_id"]; chain.append(b["version_id"])
    # repeated edits really chained (each derived from the previous)
    fold = next(f for f in call("GET", "/api/folders")[1]["items"] if f["document_id"] == d)
    res["lineage"] = [(x["number"], x["parent_number"], x["outcome"]) for x in fold["versions"]]
    # cumulative: v1 -> last derivative
    if chain:
        audit_pair(data, get(chain[-1]), [], res["checks"], "cumulative_vs_v1_structure_only")
        for k in ("pixels_outside_permitted_regions", "unedited_and_edited_page_text_integrity", "new_text_found_by_pdfium_search", "old_wording_residue"):
            res["checks"]["cumulative_vs_v1_structure_only"].pop(k, None)     # content differs by design after edits; structure/fonts/qpdf only
    # multi-edit batch from v1
    edits, meta = [], []
    for kind in ("heading", "text_span", "table_value"):
        page, r = pick(v1, kind)
        if any(e["id"] == r["id"] for e in edits): continue
        new = r["text"].rstrip()[:-1] if len(r["text"].rstrip()) > 6 else r["text"]
        if new == r["text"]: continue
        edits.append({"id": r["id"], "old": r["text"], "new": new}); meta.append({"page": page, "old": r["text"], "new": new, "bbox": r["bbox"]})
    s, b = call("POST", f"/api/documents/{d}/versions/{v1}/save", json.dumps({"pdf_edits": edits}).encode(), {"Content-Type": "application/json"})
    if s == 200:
        audit_pair(data, get(b["version_id"]), meta, res["checks"], f"batch_{len(edits)}_edits_from_v1")
    else:
        res["checks"]["batch"] = {"save": f"{s} {(b or {}).get('code')}"}
    # text that cannot fit
    page, r = pick(v1, "text_span")
    s, b = call("POST", f"/api/documents/{d}/versions/{v1}/save", json.dumps({"pdf_edits": [{"id": r["id"], "old": r["text"], "new": r["text"] + " " + "WWWWWWWWWW " * 20}]}).encode(), {"Content-Type": "application/json"})
    res["checks"]["unfittable_text"] = {"refused": s == 422 and (b or {}).get("code") in ("overflow", "glyph_unavailable"), "code": (b or {}).get("code")}
    # original unchanged; version count = chain + batch + original
    res["original_sha256_unchanged"] = hashlib.sha256(get(v1)).hexdigest() == sha
    fold = next(f for f in call("GET", "/api/folders")[1]["items"] if f["document_id"] == d)
    res["versions_after"] = len(fold["versions"])
    return res


if __name__ == "__main__":
    allr = []
    for p in sys.argv[1:]:
        r = run(Path(p)); allr.append(r)
        bad = []
        for label, c in r["checks"].items():
            for k, v in c.items():
                if k in ("qpdf_check", "pypdf_strict_reopen", "structure_preserved", "fonts_original_kept", "fonts_added_only_standard_substitutes", "unedited_and_edited_page_text_integrity", "new_text_found_by_pdfium_search", "refused") and v is not True: bad.append(f"{label}.{k}={v}")
                if k in ("pixels_outside_permitted_regions", "old_wording_residue", "clipped_outside_page", "overlapping_foreign_characters") and v: bad.append(f"{label}.{k}={v}")
                if k == "save" and not str(v).startswith("200"): bad.append(f"{label}.save={v}")
        print(Path(p).name[:22].ljust(22), "orig_sha_ok", r["original_sha256_unchanged"], "| versions", r["versions_after"], "| lineage", r["lineage"], "| FINDINGS:", bad or "none")
    json.dump(allr, open(OUT / "pdf.json", "w"), indent=1, default=str)
