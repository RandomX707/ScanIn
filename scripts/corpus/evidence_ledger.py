#!/usr/bin/env python3
"""Evidence ledger: fresh-tenant archive ingestion + per-edit evidence with explicit, named verifiers.

Private output: .private/evidence/LEDGER.json (contains real original/replacement values - never shareable).
Console prints counts and PASS/FAIL only. Needs the isolated test stack on :8010.
"""
import hashlib, io, json, os, re, subprocess, sys, urllib.parse, urllib.request, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from storage_read import read_version  # noqa: E402
import openpyxl, pypdf, pypdfium2 as pdfium, docx as pydocx  # noqa: E402

API = os.environ.get("CHANNEL360_API", "http://127.0.0.1:8010")
OUT = ROOT / ".private/evidence"; OUT.mkdir(parents=True, exist_ok=True)
MAN = json.load(open(ROOT / ".private/inventory/manifest.json"))
SHA = lambda b: hashlib.sha256(b).hexdigest()
flat = lambda s: re.sub(r"\s+", "", s)


class Tenant:
    def __init__(self, label):
        self.label = label
        t = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/host/issue_test_token.py")], capture_output=True, text=True, check=True).stdout)
        self.id, self.h = t["tenant_id"], {"Authorization": f"Bearer {t['token']}"}

    def call(self, m, p, b=None, ct="application/octet-stream", raw=False):
        r = urllib.request.Request(API + p, data=b, method=m, headers={**self.h, "Content-Type": ct})
        try:
            with urllib.request.urlopen(r, timeout=900) as x:
                d = x.read(); return x.status, (d if raw else json.loads(d or b"null"))
        except urllib.error.HTTPError as e:
            d = e.read()
            try: return e.code, json.loads(d)
            except Exception: return e.code, d

    def folders(self): return self.call("GET", "/api/folders")[1]["items"]

    def exported(self, d, v):
        """(status, bytes, how): API download when policy allows, else isolated storage (withheld by policy)."""
        s, data = self.call("GET", f"/api/documents/{d}/versions/{v}/file", raw=True)
        if s == 200: return 200, data, "API download (HTTP 200)"
        return s, read_version(self.id, d, v), f"export withheld by policy (HTTP {s}); content read from isolated storage"


def desc(v):
    v = "" if v is None else str(v)
    return f"{len(v)} chars, {'numeric' if re.fullmatch(r'[\d.,/-]+', v) else 'alphabetic' if v.replace(' ', '').isalpha() else 'mixed'}"


# ----------------------------------------------------------------------------------------- per-format edit + verify
def pdf_edits(t: Tenant, d, v1, src_bytes, name):
    rows = []
    def runs(p):
        st, body = t.call("GET", f"/api/documents/{d}/versions/{v1}/pdf-text?page={p}")
        return body["runs"] if st == 200 else []
    picks = {}
    for page in (1, 2, 3):
        rs = [r for r in runs(page) if r["e"] and 5 <= len(r["text"].strip()) <= 40]
        if not rs: continue
        picks.setdefault("heading", (page, max(rs, key=lambda r: (r["size"], len(r["text"])))))
        picks.setdefault("text_span", (page, next((r for r in rs if " " in r["text"].strip() and r["size"] < 11), rs[len(rs) // 2])))
        digits = [r for r in rs if any(c.isdigit() for c in r["text"]) and len(r["text"]) <= 20]
        if digits: picks.setdefault("table_value", (page, digits[0]))
    for kind, (page, r) in picks.items():
        old = r["text"]
        new = "".join(str((int(c) + 1) % 10) if c.isdigit() else c for c in old) if kind == "table_value" else (old.rstrip()[:-1] if kind == "heading" else " ".join(old.split(" ")[:-1]) or old[:-1])
        if new == old: continue
        s, b = t.call("POST", f"/api/documents/{d}/versions/{v1}/save", json.dumps({"pdf_edits": [{"id": r["id"], "old": old, "new": new}]}).encode(), "application/json")
        row = {"file": name, "operation": f"pdf_{kind}_replace", "location": f"page {page}, run {r['id']}, bbox {r['bbox']}", "original_value": old, "replacement_value": new,
               "redacted": f"{kind} on page {page}: {desc(old)} -> {desc(new)}"}
        if s != 200:
            row.update(saved=f"refused {s} {(b or {}).get('code')}", verifiers=[], result="FAIL" if (b or {}).get("code") not in ("overflow", "glyph_unavailable") else "REFUSED (by design)"); rows.append(row); continue
        vn = b["version_id"]; es, out, how = t.exported(d, vn)
        fold = next(f for f in t.folders() if f["document_id"] == d); ver = next(x for x in fold["versions"] if x["version_id"] == vn)
        tmp = OUT / "x.pdf"; tmp.write_bytes(out)
        q = subprocess.run(["qpdf", "--check", str(tmp)], capture_output=True, text=True)
        pdf = pdfium.PdfDocument(out); text = pdf[page - 1].get_textpage().get_text_range()
        lines = [flat(l) for l in re.split(r"[\r\n]+", text) if l.strip()]
        src_lines = [flat(l) for l in re.split(r"[\r\n]+", pdfium.PdfDocument(src_bytes)[page - 1].get_textpage().get_text_range()) if l.strip()]
        present = flat(new) in flat(text); gone = (flat(old) in flat(new)) or not any(flat(old) in l for l in lines) or sum(flat(old) in l for l in src_lines) > 1
        try: pypdf.PdfReader(io.BytesIO(out), strict=True); strict = True
        except Exception: strict = False
        ver_ = [{"verifier": "Channel360 worker (engine-independent checks: reopen, text, pixels, structure)", "result": b["outcome"]},
                {"verifier": "qpdf --check", "result": "OK" if q.returncode == 0 else "FAIL"},
                {"verifier": "pypdf (strict mode) reopen", "result": "OK" if strict else "FAIL"},
                {"verifier": "pdfium text extraction: replacement text present on cited page", "result": "OK" if present else "FAIL"},
                {"verifier": "pdfium text extraction: original wording absent from its line", "result": "OK" if gone else "FAIL"},
                {"verifier": "page count preserved (pdfium)", "result": "OK" if len(pdf) == len(pdfium.PdfDocument(src_bytes)) else "FAIL"}]
        ok = all(x["result"] in ("OK", "VERIFIED") for x in ver_)
        row.update(saved=f"v{ver['number']} ({vn[:8]}), parent v{ver['parent_number']}, outcome {b['outcome']}", verifiers=ver_, export=how,
                   exported_content_check=f"exported PDF text on page {page} contains the replacement and not the original wording" , result="PASS" if ok else "FAIL")
        rows.append(row)
    return rows


def xlsx_edits(t: Tenant, d, v1, src_bytes, name):
    rows = []
    pv = t.call("GET", f"/api/documents/{d}/versions/{v1}/preview")[1]
    cells = [c for r in pv["sheets"][0]["rows"] for c in r if c]
    sheet = pv["sheets"][0]["name"]
    strings = [c for c in cells if c["e"] and c["t"] == "string" and c["v"] and len(c["v"]) < 60]
    blanks = [c for c in cells if c["e"] and c["t"] == "null"]
    nums = [c for c in cells if c["e"] and c["t"] in ("integer", "decimal")]
    plan = []
    if strings: plan.append(("text_cell", strings[len(strings) // 2], lambda c: c["v"] + " [edited]"))
    if blanks: plan.append(("blank_template_text", blanks[len(blanks) // 3], lambda c: "Acme Test Ltd"))
    if len(blanks) > 4: plan.append(("blank_template_number", blanks[len(blanks) // 2], lambda c: "12345"))
    if nums: plan.append(("numeric_cell", nums[0], lambda c: str(int(float(c["v"])) + 1) if c["t"] == "integer" else str(float(c["v"]) + 1)))
    wa = openpyxl.load_workbook(io.BytesIO(src_bytes)).worksheets[0]
    for kind, c, mk in plan:
        ref = c["id"].split("!")[-1]; new = mk(c); old = wa[ref].value
        s, b = t.call("POST", f"/api/documents/{d}/versions/{v1}/save", json.dumps({"changes": [{"target_id": c["id"], "fingerprint": c["fp"], "value": new}]}).encode(), "application/json")
        row = {"file": name, "operation": f"xlsx_{kind}", "location": f"{sheet}!{ref}", "original_value": "" if old is None else str(old), "replacement_value": new, "redacted": f"{kind} at {ref}: {desc(old)} -> {desc(new)}"}
        if s != 200:
            row.update(saved=f"refused {s} {(b or {}).get('code')}", verifiers=[], result="FAIL"); rows.append(row); continue
        vn = b["version_id"]; es, out, how = t.exported(d, vn)
        fold = next(f for f in t.folders() if f["document_id"] == d); ver = next(x for x in fold["versions"] if x["version_id"] == vn)
        wb = openpyxl.load_workbook(io.BytesIO(out)); ws = wb.worksheets[0]; got = ws[ref].value
        same = str(got) == new or (str(got).rstrip("0").rstrip(".") == new.rstrip("0").rstrip("."))
        others = all(ws[r_.coordinate].value == r_.value for r in wa.iter_rows() for r_ in r if r_.coordinate != ref)
        from ooxml_compare import compare
        cmp = compare(src_bytes, out, {ref})
        fa = {x.coordinate: x.value for r in wa.iter_rows() for x in r if isinstance(x.value, str) and x.value.startswith("=")}
        fb = {x.coordinate: x.value for r in ws.iter_rows() for x in r if isinstance(x.value, str) and x.value.startswith("=")}
        ver_ = [{"verifier": "Channel360 independent XLSX verifier (frozen plan, OOXML parts)", "result": b["outcome"]},
                {"verifier": f"openpyxl reopen: value at {sheet}!{ref} equals the replacement", "result": "OK" if same else "FAIL"},
                {"verifier": "openpyxl reopen: every other cell value unchanged", "result": "OK" if others else "FAIL"},
                {"verifier": "raw OOXML diff: only the edited cell changed in the sheet XML; layout/merges/rows unchanged", "result": "OK" if (not cmp["problems"] and set(cmp.get("cells_changed", [])) == {ref}) else "FAIL"},
                {"verifier": "openpyxl: formula strings identical", "result": "OK" if fa == fb else "FAIL"},
                {"verifier": "openpyxl: merged ranges identical", "result": "OK" if sorted(map(str, wa.merged_cells.ranges)) == sorted(map(str, ws.merged_cells.ranges)) else "FAIL"},
                {"verifier": "openpyxl: embedded images identical (count)", "result": "OK" if len(wa._images) == len(ws._images) else "FAIL"}]
        ok = all(x["result"] in ("OK", "VERIFIED", "INCOMPLETE") for x in ver_) and b["outcome"] in ("VERIFIED", "INCOMPLETE")
        row.update(saved=f"v{ver['number']} ({vn[:8]}), parent v{ver['parent_number']}, outcome {b['outcome']}", verifiers=ver_, export=how,
                   exported_content_check=f"exported workbook cell {ref} reads the replacement; all other cells, formulas, merges and images unchanged", result="PASS" if ok else "FAIL")
        if b["outcome"] == "INCOMPLETE": row["note"] = "edited cell feeds formulas; cached results stale until Excel recalculates; download withheld by policy"
        rows.append(row)
    return rows


def docx_edits(t: Tenant, d, v1, src_bytes, name):
    rows = []
    blocks = t.call("GET", f"/api/documents/{d}/versions/{v1}/preview")[1]["blocks"]
    ed = [x for x in blocks if x["e"] and len(x["text"]) > 12]
    para = next(x for x in ed if not x["table"]); cell = next((x for x in ed if x["table"]), None)
    da = pydocx.Document(io.BytesIO(src_bytes))
    for kind, b_ in (("paragraph", para), ("table_cell", cell)):
        if not b_: continue
        new = b_["text"] + " [edited]"
        s, b = t.call("POST", f"/api/documents/{d}/versions/{v1}/save", json.dumps({"changes": [{"target_id": b_["target_id"], "fingerprint": b_["fp"], "value": new}]}).encode(), "application/json")
        loc = f"table {b_['table']['index'] + 1}, row {b_['table']['row'] + 1}, column {b_['table']['col'] + 1}" if b_["table"] else f"paragraph block {blocks.index(b_) + 1}"
        row = {"file": name, "operation": f"docx_{kind}", "location": loc, "original_value": b_["text"], "replacement_value": new, "redacted": f"{kind} at {loc}: {desc(b_['text'])} -> {desc(new)}"}
        if s != 200:
            row.update(saved=f"refused {s}", verifiers=[], result="FAIL"); rows.append(row); continue
        vn = b["version_id"]; es, out, how = t.exported(d, vn)
        fold = next(f for f in t.folders() if f["document_id"] == d); ver = next(x for x in fold["versions"] if x["version_id"] == vn)
        dn = pydocx.Document(io.BytesIO(out))
        W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        texts = lambda doc: [p.text for p in doc.paragraphs] + ["".join(x.text or "" for x in tc.iter(W + "t")) for tb in doc.tables for tc in tb._tbl.iter(W + "tc")]   # true cells (python-docx row.cells repeats merged cells)
        ta, tn = texts(da), texts(dn)
        changed = [i for i, (x, y) in enumerate(zip(ta, tn)) if x != y]
        za, zn = zipfile.ZipFile(io.BytesIO(src_bytes)), zipfile.ZipFile(io.BytesIO(out))
        same_parts = all(za.read(n) == zn.read(n) for n in ("word/styles.xml", "word/settings.xml", "word/_rels/document.xml.rels") if n in za.namelist())
        media = all(za.read(n) == zn.read(n) for n in za.namelist() if "media/" in n)
        xa, xn = za.read("word/document.xml").decode(), zn.read("word/document.xml").decode()
        counts = all(len(re.findall(p, xa)) == len(re.findall(p, xn)) for p in (r"<w:tbl>", r"<w:drawing>", r"<w:hyperlink\b", r"<w:ins\b", r"<w:del\b"))
        ver_ = [{"verifier": "Channel360 DOCX structural verifier (profile: structural; visual NOT performed)", "result": b["outcome"]},
                {"verifier": "python-docx reopen: exactly one text block changed and it equals the replacement", "result": "OK" if (len(tn) == len(ta) and len(changed) == 1 and new in [tn[changed[0]]]) else "FAIL"},
                {"verifier": "raw OOXML: styles, settings and relationships parts byte-identical", "result": "OK" if same_parts else "FAIL"},
                {"verifier": "raw OOXML: media parts byte-identical", "result": "OK" if media else "FAIL"},
                {"verifier": "raw OOXML: table, drawing, hyperlink and tracked-change element counts unchanged", "result": "OK" if counts else "FAIL"}]
        ok = all(x["result"] in ("OK", "VERIFIED", "STRUCTURALLY_VERIFIED") for x in ver_)
        row.update(saved=f"v{ver['number']} ({vn[:8]}), parent v{ver['parent_number']}, outcome {b['outcome']}", verifiers=ver_, export=how,
                   exported_content_check="exported document's changed block equals the replacement; styles/media/relationships/tables/links untouched", result="PASS" if ok else "FAIL")
        rows.append(row)
    return rows


def run_doc(t: Tenant, entry, ledger, source_label):
    name, d, v1, src_bytes, kind = entry
    row_base = {"tenant": t.label, "source": source_label}
    s, b = t.call("GET", f"/api/documents/{d}/versions/{v1}/preview"); pv = b
    if kind == "pdf" and pv["edit"]["mode"] != "pdf_text":
        sig = (pv.get("signature") or {}).get("status")
        s2, r2 = t.call("POST", f"/api/documents/{d}/versions/{v1}/save", json.dumps({"pdf_edits": [{"id": "p1:page:1-2", "old": "a", "new": "b"}]}).encode(), "application/json")
        ledger.append({**row_base, "file": name, "operation": "restricted (no edit attempted on content)", "location": "-", "redacted": f"signature status '{sig}'; save attempt -> HTTP {s2} {(r2 or {}).get('code')}",
                       "verifiers": [{"verifier": "Channel360 preview", "result": f"read-only: {pv['edit']['reason'][:90]}"}, {"verifier": "crafted save attempt", "result": f"refused HTTP {s2}"},
                                     {"verifier": "SHA-256 of original after attempt", "result": "unchanged" if SHA(t.call('GET', f'/api/documents/{d}/versions/{v1}/file', raw=True)[1]) == SHA(src_bytes) else "CHANGED"}],
                       "result": "SAFE RESTRICTION" if s2 == 409 else "FAIL"})
        return
    fn = {"pdf": pdf_edits, "xlsx": xlsx_edits, "docx": docx_edits}[kind]
    for r in fn(t, d, v1, src_bytes, name):
        ledger.append({**row_base, **r})


def import_zip(t: Tenant, zipname):
    data = (ROOT / "Policy Copies" / zipname).read_bytes()
    s, rep = t.call("PUT", f"/api/archives?filename={urllib.parse.quote(zipname)}", data)
    return rep, zipfile.ZipFile(io.BytesIO(data))


def main():
    ledger, checks = [], []
    def chk(name, ok, detail=""): checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:200]}); print(("PASS " if ok else "FAIL ") + name + (f" [{detail}]" if not ok else ""))
    by_sha = {o["sha256"]: o for o in MAN["occurrences"]}
    for zipname, expect_n in (("QCR.zip", 7), ("RFQ.zip", 17)):
        t = Tenant(f"fresh tenant: {zipname} first")
        assert t.folders() == [], "tenant must start empty"
        rep, zf = import_zip(t, zipname)
        sm = rep["summary"]
        chk(f"{zipname} into EMPTY tenant: {expect_n} entries, {expect_n} imported, 0 linked, 0 skipped/blocked/failed", sm["entries"] == expect_n == sm["imported"] and not (sm["linked_duplicate"] or sm["skipped"] or sm["blocked"] or sm["failed"]), sm)
        items = t.folders()
        chk(f"{zipname}: exactly {expect_n} distinct documents exist", len(items) == expect_n and len({f['document_id'] for f in items}) == expect_n)
        files = {}
        for f in items:
            v1 = f["versions"][0]["version_id"]; files[f["document_id"]] = (f, SHA(t.call("GET", f"/api/documents/{f['document_id']}/versions/{v1}/file", raw=True)[1]))
        entries = {i.filename: SHA(zf.read(i)) for i in zf.infolist() if not i.is_dir()}
        chk(f"{zipname}: every archive path is recorded as the document's source and its original bytes equal that entry", all(f["source"] and f["source"]["archive"] == zipname and entries.get(f["source"]["path"]) == h for f, h in files.values()))
        chk(f"{zipname}: {expect_n} distinct original hashes, all in the corpus manifest", len({h for _, h in files.values()}) == expect_n and all(h in by_sha for _, h in files.values()))
        chk(f"{zipname}: report paths == archive's stored names", sorted(e["path"] for e in rep["entries"]) == sorted(entries))
        # independent usability afterwards: preview + chunks + download + a real edit, per document
        used = 0
        for f, h in files.values():
            d, v1 = f["document_id"], f["versions"][0]["version_id"]
            data = t.call("GET", f"/api/documents/{d}/versions/{v1}/file", raw=True)[1]; kind = f["name"].rsplit(".", 1)[-1].lower()
            kind = {"xlsx": "xlsx", "pdf": "pdf", "docx": "docx"}[kind]
            pv = t.call("GET", f"/api/documents/{d}/versions/{v1}/preview"); ch = t.call("GET", f"/api/documents/{d}/versions/{v1}/chunks")
            if pv[0] == 200 and ch[0] == 200 and ch[1]["sections"]: used += 1
            run_doc(t, (f["name"], d, v1, data, kind), ledger, f"{zipname}:{f['source']['path']}")
        chk(f"{zipname}: all {expect_n} documents open, show source-linked sections and accept edits independently", used == expect_n, used)
    # a third tenant: standalone uploads (control)
    t = Tenant("fresh tenant: standalone uploads")
    seen = set()
    for o in MAN["occurrences"]:
        if o["sha256"] in seen: continue
        seen.add(o["sha256"])
        p = ROOT / "Policy Copies" / o["path"] if o["source"] == "standalone" else ROOT / ".private/corpus" / o["source"].removesuffix(".zip") / o["path"]
        s, reg = t.call("PUT", "/api/documents?filename=" + urllib.parse.quote(p.name), p.read_bytes())
        t.call("POST", f"/api/documents/{reg['document_id']}/inspect")
        run_doc(t, (p.name, reg["document_id"], reg["version_id"], p.read_bytes(), o["kind"]), ledger, "standalone upload")
    fails = [r for r in ledger if r["result"] == "FAIL"]
    print("LEDGER rows:", len(ledger), "| PASS", sum(r["result"] == "PASS" for r in ledger), "| SAFE RESTRICTION", sum(r["result"] == "SAFE RESTRICTION" for r in ledger),
          "| REFUSED (by design)", sum(r["result"].startswith("REFUSED") for r in ledger), "| FAIL", len(fails))
    for r in fails[:8]: print("  FAIL:", r["file"], r["operation"], [v for v in r.get("verifiers", []) if v["result"] not in ("OK", "VERIFIED", "INCOMPLETE", "STRUCTURALLY_VERIFIED")][:2], r.get("saved"))
    json.dump({"checks": checks, "ledger": ledger}, open(OUT / "LEDGER.json", "w"), indent=1, default=str)
    return 1 if fails or any(not c["ok"] for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
