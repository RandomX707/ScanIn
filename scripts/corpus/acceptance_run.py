#!/usr/bin/env python3
"""M7: full stakeholder acceptance run through the REAL API in fresh isolated tenants. Private output only.

Writes .private/inventory/ACCEPTANCE_RESULTS.md (+ .json). Console output is verdicts and check names only.
Verdicts: PASS | SAFE RESTRICTION | FAIL.  Needs the isolated test stack (scripts/dev/start-test-stack.sh) and the private
corpus + `scripts/corpus/inventory.py` manifest. Nothing here writes to the demo tenant.
"""
import argparse, hashlib, io, json, os, random, re, subprocess, sys, time, urllib.parse, urllib.request, zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ooxml_compare import compare as compare_xlsx  # noqa: E402

API = os.environ.get("CHANNEL360_API", "http://127.0.0.1:8010")
if API.rstrip("/").endswith((":8000", ":5174")):
    raise SystemExit("REFUSED: the acceptance runner uploads the whole corpus and must run against the isolated stack, never the shared app")
OUT = Path(os.environ.get("CHANNEL360_RESULTS_DIR") or (ROOT / ".private/inventory"))
MANIFEST = json.load(open(ROOT / ".private/inventory/manifest.json"))
PY = sys.executable
# The original QCR.zip / RFQ.zip must sit in "Policy Copies". If they are gone, CHANNEL360_ZIP_DIR may point at archives rebuilt with
# scripts/corpus/rebuild_archives.py; the result is then labelled RECONSTRUCTED so it is never mistaken for the original archives.
ZIP_DIR = Path(os.environ.get("CHANNEL360_ZIP_DIR") or (ROOT / "Policy Copies"))
ZIP_RECONSTRUCTED = bool(os.environ.get("CHANNEL360_ZIP_DIR"))
ISO_DB = os.environ.get("C360_ISO_DB", "channel360_iso_dev")
if not ISO_DB.startswith("channel360_iso_"):
    raise SystemExit("REFUSED: the acceptance runner only reads the isolated channel360_iso_* database")
ISO_PGPASS = ROOT / ".runtime/iso-stack" / ("pgpass" if ISO_DB == "channel360_iso_dev" else f"pgpass.{ISO_DB}")


def new_tenant():
    tok = json.loads(subprocess.run([PY, str(ROOT / "scripts/dev/issue_iso_token.py")], capture_output=True, text=True, check=True).stdout)
    return tok


class Client:
    def __init__(self):
        self.tok = new_tenant()
        self.h = {"Authorization": f"Bearer {self.tok['token']}"}

    def call(self, method, path, body=None, headers=None, raw=False):
        req = urllib.request.Request(API + path, data=body, method=method, headers={**self.h, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                data = r.read()
                return r.status, (data if raw else (json.loads(data) if data else None)), dict(r.headers)
        except urllib.error.HTTPError as e:
            data = e.read()
            try:
                return e.code, json.loads(data), {}
            except Exception:
                return e.code, data, {}

    def upload(self, path: Path):
        s, j, _ = self.call("PUT", "/api/documents?filename=" + urllib.parse.quote(path.name), path.read_bytes(), {"Content-Type": "application/octet-stream"})
        if s != 200:
            return s, None, None
        s2, j2, _ = self.call("POST", f"/api/documents/{j['document_id']}/inspect")
        return s2, j, j2

    def file(self, d, v):
        s, data, _ = self.call("GET", f"/api/documents/{d}/versions/{v}/file", raw=True)
        if s == 200:
            return data
        from storage_read import read_version          # INCOMPLETE derivatives are withheld from download by design
        return read_version(self.tok["tenant_id"], d, v)

    def json(self, path):
        return self.call("GET", path)[1]


def unique_docs():
    seen, out = set(), []
    for o in MANIFEST["occurrences"]:
        if o["sha256"] in seen:
            continue
        seen.add(o["sha256"])
        path = ROOT / "Policy Copies" / o["path"] if o["source"] == "standalone" else ROOT / ".private/corpus" / o["source"].removesuffix(".zip") / o["path"]
        out.append((o, path))
    return out


class Result:
    def __init__(self, name, kind):
        self.name, self.kind, self.checks = name, kind, []
        self.restriction = None
        self.timings = {}

    def check(self, name, ok, detail=""):
        self.checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:220]})
        return ok

    @property
    def verdict(self):
        if getattr(self, "skipped", False):
            return "SKIPPED — OUT OF SCOPE"
        if any(not c["ok"] for c in self.checks):
            return "FAIL"
        return "SAFE RESTRICTION" if self.restriction else "PASS"


def native_cell_value_matches(original_type, actual, expected):
    """Excel may store a numeric string entered into a blank cell as a number; existing text must remain exact."""
    if actual == expected:
        return True
    if original_type != "null" or isinstance(actual, bool):
        return False
    try:
        return Decimal(str(actual)) == Decimal(str(expected))
    except (InvalidOperation, ValueError):
        return False


def skipped_zip_result():
    result = Result("ZIP import", "zip")
    result.skipped = True
    return result


def timed(res, key, fn):
    t = time.time()
    out = fn()
    res.timings[key] = round(time.time() - t, 2)
    return out


def run_pdf(cl: Client, o, path: Path, res: Result):
    data = path.read_bytes()
    sha = o["sha256"]
    st, reg, insp = timed(res, "import_inspect_s", lambda: cl.upload(path))
    if not res.check("import", st == 200 and reg and insp and insp.get("state") == "ready", f"inspect={st}"):
        return
    d, v = reg["document_id"], reg["version_id"]
    res.check("original_download_sha256", hashlib.sha256(cl.file(d, v)).hexdigest() == sha)
    pv = cl.json(f"/api/documents/{d}/versions/{v}/preview")
    res.check("preview_readable", pv["format"] == "pdf" and len(pv.get("pages", [])) == o["pages"], f"pages {len(pv.get('pages', []))}/{o['pages']}")
    ch = timed(res, "chunks_s", lambda: cl.json(f"/api/documents/{d}/versions/{v}/chunks"))
    secs = ch["sections"]
    res.check("chunks_meaningful", 1 <= len(secs) <= 12 and all(s.get("source") and s["points"] for s in secs), f"{len(secs)} sections")
    res.check("chunks_source_linked", all((s["source"] or {}).get("pages") for s in secs))
    sig = (pv.get("signature") or {}).get("status")
    mode = pv["edit"]["mode"]
    if sig in ("signed", "signature_range_mismatch", "modified_after_signing"):
        res.check("signed_pdf_not_editable", mode == "none" and "signature" in (pv["edit"]["reason"] or "").lower() or "signed" in (pv["edit"]["reason"] or "").lower(), pv["edit"]["reason"])
        s2, body, _ = cl.call("POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"pdf_edits": [{"id": "p1:page:1-2", "old": "x", "new": "y"}]}).encode(), {"Content-Type": "application/json"})
        res.check("signed_pdf_save_refused", s2 == 409 and (body or {}).get("code") == "pdf_signed", f"{s2} {(body or {}).get('code')}")
        res.check("signature_state_reported_not_trusted", (pv.get("signature") or {}).get("validated") is False)
        res.check("original_untouched_after_attempt", hashlib.sha256(cl.file(d, v)).hexdigest() == sha)
        # Signature status is cryptographically checked and reported truthfully; the signed original stays read-only, and the supported
        # path is an explicit, acknowledged, visibly marked UNSIGNED working copy that can be edited, exported and compared.
        state = (pv.get("signature") or {}).get("state")
        res.check("signature_state_is_cryptographic_and_never_trusted_without_policy", state in ("integrity_valid_trust_not_established", "malformed", "integrity_invalid", "modified_after_signing"), state)
        s3, wb, _ = cl.call("POST", f"/api/documents/{d}/versions/{v}/working-copy", json.dumps({}).encode(), {"Content-Type": "application/json"})
        res.check("working_copy_requires_acknowledgement", s3 == 422 and wb.get("code") == "acknowledgement_required", f"{s3} {wb.get('code')}")
        s3, wb, _ = cl.call("POST", f"/api/documents/{d}/versions/{v}/working-copy", json.dumps({"acknowledge": True}).encode(), {"Content-Type": "application/json"})
        if res.check("working_copy_created_unsigned_and_verified", s3 == 200 and wb.get("classification") == "unsigned_working_copy" and wb.get("outcome") == "VERIFIED", f"{s3} {wb.get('code') or wb.get('outcome')}"):
            wcv = wb["version_id"]; wcb = cl.file(d, wcv)
            flat = wcb.replace(b"\n", b"").replace(b"\r", b"")
            import io as _io, pypdf as _pypdf
            rd = _pypdf.PdfReader(_io.BytesIO(wcb))
            res.check("working_copy_has_no_signature_and_visible_notice_on_every_page", not any(m in flat for m in (b"/ByteRange", b"/DocMDP", b"/SigFlags", b"/Type /Sig", b"/Type/Sig"))
                      and all("NOTOFFICIAL" in (pg.extract_text() or "").replace(" ", "") for pg in rd.pages), f"{len(rd.pages)} pages")
            wpv = cl.json(f"/api/documents/{d}/versions/{wcv}/preview")
            res.check("working_copy_is_editable_text_and_classified", wpv["edit"]["mode"] == "pdf_text" and wpv["classification"] == "unsigned_working_copy" and wpv["signature"]["state"] == "none", f"{wpv['edit']['mode']} {wpv['classification']}")
            runs = []
            for page_no in range(1, min(len(rd.pages), 6) + 1):      # a policy's first page may be a graphic or a table; take the first page with plain editable lines
                page_runs = [r for r in cl.json(f"/api/documents/{d}/versions/{wcv}/pdf-text?page={page_no}")["runs"] if r["e"] and r["size"] >= 6 and 6 <= len(r["text"]) <= 40 and r["text"].replace(" ", "").isalpha()]
                runs = [r for r in page_runs if " " in r["text"]] or page_runs        # multi-word lines first; some PDFs store one word per text object
                if runs:
                    break
            if res.check("working_copy_has_editable_lines", bool(runs), f"{len(runs)} on page {page_no}"):
                r0 = runs[0]; newt = " ".join(r0["text"].split(" ")[:-1]) if " " in r0["text"] else r0["text"][:-1]
                s4, eb, _ = cl.call("POST", f"/api/documents/{d}/versions/{wcv}/save", json.dumps({"pdf_edits": [{"id": r0["id"], "old": r0["text"], "new": newt}]}).encode(), {"Content-Type": "application/json"})
                res.check("working_copy_edit_saved_as_next_version", s4 == 200 and eb.get("outcome") == "VERIFIED", f"{s4} {eb.get('code') or eb.get('outcome')}")
                cmpr = cl.json(f"/api/documents/{d}/compare?base={v}&target={eb.get('version_id')}") if s4 == 200 else {}
                res.check("compare_signed_original_vs_edited_working_copy_with_correct_lineage", s4 == 200 and cmpr.get("relation", {}).get("path_numbers") == [1, 2, 3] and cmpr["target"]["classification"] == "unsigned_working_copy", cmpr.get("relation"))
        res.check("signed_original_byte_identical_after_working_copy_and_edits", hashlib.sha256(cl.file(d, v)).hexdigest() == sha)
        fold = next(f for f in cl.json("/api/folders")["items"] if f["document_id"] == d)
        res.check("lineage_working_copy_chain", [x["parent_number"] for x in fold["versions"]] == [None, 1, 2], [x["parent_number"] for x in fold["versions"]])
        return
    if not res.check("pdf_editable_as_text", mode == "pdf_text", f"mode={mode} reason={pv['edit']['reason']}"):
        return
    pg = cl.json(f"/api/documents/{d}/versions/{v}/pdf-text?page=1")
    runs = [r for r in pg["runs"] if r["e"] and 8 <= len(r["text"]) <= 40 and r["size"] >= 7]
    res.check("selectable_text_runs", len(runs) > 5, f"{len(runs)} editable lines on page 1")
    r = runs[0]
    new = " ".join(r["text"].split(" ")[:-1]) or r["text"][:-2]
    edit = {"id": r["id"], "old": r["text"], "new": new}
    s1, prev_bytes, hdr = timed(res, "edit_preview_s", lambda: cl.call("POST", f"/api/documents/{d}/versions/{v}/pdf-edit/preview", json.dumps({"pdf_edits": [edit]}).encode(), {"Content-Type": "application/json"}, raw=True))
    res.check("preview_modified_pdf", s1 == 200 and prev_bytes[:5] == b"%PDF-", f"{s1}")
    s2, body, _ = timed(res, "save_s", lambda: cl.call("POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"pdf_edits": [edit]}).encode(), {"Content-Type": "application/json"}))
    if not res.check("save_next_version", s2 == 200 and body.get("outcome") == "VERIFIED", f"{s2} {(body or {}).get('outcome') or (body or {}).get('code')}"):
        return
    v2 = body["version_id"]
    out_bytes = cl.file(d, v2)
    tmp = OUT / "acc-saved.pdf"; tmp.write_bytes(out_bytes)
    ver = subprocess.run([PY, str(ROOT / "scripts/corpus/verify_pdf_text.py"), str(path), str(tmp), r["text"], new], capture_output=True, text=True)
    res.check("independent_reopen_text_diff_pixels", ver.stdout.strip() == "OK", (ver.stdout + ver.stderr).strip()[:120])
    flat = lambda t: re.sub(r"\s+", "", t)           # \s also covers non-breaking spaces
    res.check("native_pdf_reopens_searchable", out_bytes[:5] == b"%PDF-" and flat(new) in flat(__import__("pypdfium2").PdfDocument(out_bytes)[0].get_textpage().get_text_range()))
    res.check("v1_byte_identical", hashlib.sha256(cl.file(d, v)).hexdigest() == sha)
    fold = next(f for f in cl.json("/api/folders")["items"] if f["document_id"] == d)
    vs = fold["versions"]
    res.check("lineage_v2_parent_v1", len(vs) == 2 and vs[1]["parent_number"] == 1 and vs[1]["outcome"] == "VERIFIED", [(x["number"], x["parent_number"], x["outcome"]) for x in vs])
    audit = cl.json(f"/api/documents/{d}/audit")["items"]
    ev = [e for e in audit if e["action"] == "document.text_version_saved" and e["payload"].get("version_id") == v2]
    res.check("audit_event_recorded", len(ev) == 1 and ev[0]["payload"].get("output_sha256") == hashlib.sha256(out_bytes).hexdigest() and ev[0]["payload"].get("parent_version_id") == v and ev[0]["actor_id"], f"{len(ev)} event(s)")
    # edit the HISTORICAL version again -> v3 derived from v1
    r2 = runs[1]
    s3, b3, _ = cl.call("POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"pdf_edits": [{"id": r2["id"], "old": r2["text"], "new": r2["text"][:-1] or r2["text"]}]}).encode(), {"Content-Type": "application/json"})
    fold = next(f for f in cl.json("/api/folders")["items"] if f["document_id"] == d)
    res.check("edit_historical_version_v3_from_v1", s3 == 200 and fold["versions"][2]["parent_number"] == 1, f"{s3}")
    # overflow / unsafe edit is refused, never silently saved
    s4, b4, _ = cl.call("POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"pdf_edits": [{"id": r["id"], "old": r["text"], "new": r["text"] + " " + "WWWWWWWW " * 14}]}).encode(), {"Content-Type": "application/json"})
    res.check("overflow_refused_not_saved", s4 == 422 and (b4 or {}).get("code") in {"overflow", "glyph_unavailable"}, f"{s4} {(b4 or {}).get('code')}")
    after = next(f for f in cl.json("/api/folders")["items"] if f["document_id"] == d)
    res.check("refusal_created_no_version", len(after["versions"]) == 3)


def run_xlsx(cl: Client, o, path: Path, res: Result):
    data = path.read_bytes(); sha = o["sha256"]
    st, reg, insp = timed(res, "import_inspect_s", lambda: cl.upload(path))
    if not res.check("import", st == 200 and insp and insp.get("state") == "ready"):
        return
    d, v = reg["document_id"], reg["version_id"]
    res.check("original_download_sha256", hashlib.sha256(cl.file(d, v)).hexdigest() == sha)
    pv = cl.json(f"/api/documents/{d}/versions/{v}/preview")
    sh = pv["sheets"][0]; lay = sh.get("layout") or {}
    res.check("layout_merges_match_source", len(lay.get("merges", [])) == o["merged"], f"{len(lay.get('merges', []))}/{o['merged']}")
    res.check("layout_picture_present", len(lay.get("images", [])) == 1)
    cells = [c for row in sh["rows"] for c in row if c]
    res.check("grid_has_editable_cells", pv["edit"]["mode"] == "grid" and sum(1 for c in cells if c["e"]) > 5)
    formulas = [c for c in cells if not c["e"] and str(c["v"]).startswith("=")]
    res.check("formula_cells_shown_and_locked", len(formulas) == o["formulas"], f"{len(formulas)}/{o['formulas']}")
    ch = cl.json(f"/api/documents/{d}/versions/{v}/chunks")["sections"]
    res.check("chunks_meaningful_cell_anchored", 1 <= len(ch) <= 12 and all(s.get("source") and s["points"] for s in ch), f"{len(ch)} sections")
    blanks = [c for c in cells if c["e"] and c["t"] == "null"]
    strings = [c for c in cells if c["e"] and c["t"] == "string" and c["v"] and len(c["v"]) < 60]
    rng = random.Random(4)
    picks = rng.sample(strings, min(1, len(strings))) + rng.sample(blanks, min(2, len(blanks)))
    res.check("has_text_and_blank_fields_to_edit", len(picks) >= 2, f"{len(strings)} text, {len(blanks)} blank")
    changes = []
    for i, c in enumerate(picks):
        val = c["v"] + " x" if c["t"] == "string" else ["Acme Test Ltd", "12345"][i % 2]
        changes.append({"target_id": c["id"], "fingerprint": c["fp"], "value": val})
    s2, body, _ = timed(res, "save_s", lambda: cl.call("POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"changes": changes}).encode(), {"Content-Type": "application/json"}))
    if not res.check("save_next_version", s2 == 200 and body.get("outcome") in {"VERIFIED", "INCOMPLETE"}, f"{s2} {(body or {}).get('outcome') or (body or {}).get('code')}"):
        return
    if o["formulas"]:
        res.check("formula_caveat_disclosed", body.get("outcome") == "INCOMPLETE" and "Formulas update" in (body.get("note") or "") or body.get("outcome") == "VERIFIED", f"{body.get('outcome')} {body.get('note')}")
    new = cl.file(d, body["version_id"])
    edited = {i["target_id"].split("!")[-1] for i in changes}
    cmp = compare_xlsx(data, new, edited)
    res.check("package_only_edited_cells_changed", not cmp["problems"] and set(cmp.get("cells_changed", [])) == edited, f"{cmp['problems']} {cmp.get('cells_changed')}")
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(new))
    original_types = {cell["id"]: cell["t"] for cell in picks}
    observed = [(item["target_id"].split("!")[-1], wb[sh["name"]][item["target_id"].split("!")[-1]].value,
                 item["value"], original_types[item["target_id"]]) for item in changes] if sh["name"] in wb.sheetnames else []
    matches = bool(observed) and all(native_cell_value_matches(cell_type, value, expected)
                                     for _, value, expected, cell_type in observed)
    res.check("native_xlsx_reopens_with_edited_values", matches,
              observed if observed and not matches else "")
    res.check("v1_byte_identical", hashlib.sha256(cl.file(d, v)).hexdigest() == sha)
    fold = next(f for f in cl.json("/api/folders")["items"] if f["document_id"] == d)
    res.check("lineage_v2_parent_v1", fold["versions"][1]["parent_number"] == 1, [(x["number"], x["parent_number"], x["outcome"]) for x in fold["versions"]])
    audit = cl.json(f"/api/documents/{d}/audit")["items"]
    res.check("audit_event_recorded", any("version" in e["action"] and "release" in e["action"] or e["action"] == "document.derived_version_released" for e in audit), f"{len(audit)} events")
    pv2 = cl.json(f"/api/documents/{d}/versions/{body['version_id']}/preview")
    res.check("saved_version_keeps_layout", len((pv2["sheets"][0].get("layout") or {}).get("merges", [])) == o["merged"])


def run_docx(cl: Client, o, path: Path, res: Result):
    data = path.read_bytes(); sha = o["sha256"]
    st, reg, insp = timed(res, "import_inspect_s", lambda: cl.upload(path))
    if not res.check("import", st == 200 and insp and insp.get("state") == "ready"):
        return
    d, v = reg["document_id"], reg["version_id"]
    res.check("original_download_sha256", hashlib.sha256(cl.file(d, v)).hexdigest() == sha)
    pv = cl.json(f"/api/documents/{d}/versions/{v}/preview")
    blocks = pv.get("blocks", [])
    res.check("blocks_and_table_shown", len(blocks) > 20 and any(b["table"] for b in blocks), f"{len(blocks)} blocks")
    ch = cl.json(f"/api/documents/{d}/versions/{v}/chunks")["sections"]
    res.check("chunks_source_linked", len(ch) >= 1 and all(s.get("source") for s in ch), f"{len(ch)} sections")
    res.check("tracking_setting_not_treated_as_revision", pv["edit"]["mode"] == "docx", pv["edit"]["reason"])
    ed = [b for b in blocks if b["e"] and len(b["text"]) > 12]
    res.check("bounded_text_blocks_editable", len(ed) > 10, f"{len(ed)} of {len(blocks)}")
    blk = ed[len(ed) // 2]
    s2, body, _ = cl.call("POST", f"/api/documents/{d}/versions/{v}/save", json.dumps({"changes": [{"target_id": blk["target_id"], "fingerprint": blk["fp"], "value": blk["text"] + " e2e"}]}).encode(), {"Content-Type": "application/json"})
    if not res.check("save_next_version", s2 == 200 and body.get("outcome") in {"VERIFIED", "STRUCTURALLY_VERIFIED"}, f"{s2} {(body or {}).get('outcome') or (body or {}).get('code')}"):
        return
    res.check("tracking_note_disclosed", "Track Changes" in (body.get("note") or ""))
    new = cl.file(d, body["version_id"])
    def stats(b):
        z = zipfile.ZipFile(io.BytesIO(b)); x = z.read("word/document.xml").decode()
        return {"tables": len(re.findall(r"<w:tbl>", x)), "drawings": len(re.findall(r"<w:drawing>", x)), "links": len(re.findall(r'TargetMode="External"', z.read("word/_rels/document.xml.rels").decode())),
                "ins": len(re.findall(r"<w:ins\b", x)), "del": len(re.findall(r"<w:del\b", x)), "track": b"trackRevisions" in z.read("word/settings.xml"),
                "media": sorted(n for n in z.namelist() if "media" in n and not n.endswith("/")), "hdrftr": sorted(n for n in z.namelist() if re.match(r"word/(header|footer)", n))}
    a, b = stats(data), stats(new)
    res.check("table_images_links_headers_media_preserved", a == b, {k: (a[k], b[k]) for k in a if a[k] != b[k]})
    res.check("revision_markup_not_added_or_removed", (a["ins"], a["del"], a["track"]) == (b["ins"], b["del"], b["track"]))
    import docx as pydocx
    res.check("native_docx_reopens", len(pydocx.Document(io.BytesIO(new)).paragraphs) > 20)
    res.check("v1_byte_identical", hashlib.sha256(cl.file(d, v)).hexdigest() == sha)
    fold = next(f for f in cl.json("/api/folders")["items"] if f["document_id"] == d)
    res.check("lineage_v2_parent_v1", fold["versions"][1]["parent_number"] == 1)


def run_archives(results):
    cl = Client()
    res = Result("ZIP import (QCR.zip, RFQ.zip)" + (" — RECONSTRUCTED ARCHIVES (originals absent)" if ZIP_RECONSTRUCTED else ""), "zip")
    def up(name, dup="link"):
        p = ZIP_DIR / name
        return cl.call("PUT", f"/api/archives?filename={urllib.parse.quote(name)}&duplicates={dup}", p.read_bytes(), {"Content-Type": "application/octet-stream"})
    s, qcr, _ = timed(res, "qcr_zip_s", lambda: up("QCR.zip"))
    res.check("QCR.zip recognises 7 of 7", s == 200 and qcr["summary"]["recognized"] == 7 and qcr["summary"]["imported"] == 7, qcr["summary"] if s == 200 else s)
    s, rfq, _ = timed(res, "rfq_zip_s", lambda: up("RFQ.zip"))
    res.check("RFQ.zip recognises 17 of 17", s == 200 and rfq["summary"]["recognized"] == 17, rfq["summary"] if s == 200 else s)
    res.check("duplicates linked not re-added (7)", rfq["summary"]["linked_duplicate"] == 7 and rfq["summary"]["imported"] == 10)
    res.check("folder paths preserved", {e["folder"] for e in rfq["entries"]} == {"RFQ", "QCR", "Policy Copies"})
    names = {e["name"] for e in rfq["entries"]}
    res.check("unicode / odd names exact", "Marine  RFQ.xlsx" in names and "Motor  Policy.pdf" in names and "GMC RFQ.XLSX" in names)
    items = cl.json("/api/folders")["items"]
    res.check("tenant holds 17 unique documents", len(items) == 17, len(items))
    res.check("every document has archive provenance", all(f.get("source") for f in items))
    # standalone duplicate disclosure
    s, j, _ = cl.call("PUT", "/api/documents?filename=" + urllib.parse.quote("CAR Policy copy.pdf"), (ROOT / "Policy Copies/CAR Policy copy.pdf").read_bytes(), {"Content-Type": "application/octet-stream"})
    res.check("standalone duplicate of an archive document is disclosed", s == 200 and bool(j.get("duplicate_of")), (j or {}).get("duplicate_of"))
    # hostile archive
    hz = OUT / "acc-hostile.zip"
    subprocess.run([PY, str(ROOT / "scripts/corpus/make_hostile_zip.py"), str(hz)], check=True)
    s, h, _ = cl.call("PUT", "/api/archives?filename=hostile.zip", hz.read_bytes(), {"Content-Type": "application/octet-stream"})
    want = {"../evil.csv": "unsafe_path", "link.csv": "symlink", "nested/inner.zip": "nested_archive", "secret.csv": "encrypted_entry", "bomb.csv": "entry_too_large", "tools/run.exe": "unsupported_type"}
    got = {e["path"]: e["reason"] for e in (h or {}).get("entries", [])}
    res.check("hostile archive: every entry accounted for with the right reason", s == 200 and all(got.get(k) == v for k, v in want.items()) and h["summary"]["imported"] == 2, got)
    return res, cl


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run isolated stakeholder corpus acceptance.")
    parser.add_argument("--skip-zip", action="store_true", help="skip archive-specific acceptance and report it out of scope")
    args = parser.parse_args(argv)
    results = []
    os.makedirs(OUT, exist_ok=True)
    docs = unique_docs()
    for o, path in docs:
        cl = Client()
        res = Result(o["path"], o["kind"])
        try:
            {"pdf": run_pdf, "xlsx": run_xlsx, "docx": run_docx}[o["kind"]](cl, o, path, res)
        except Exception as exc:  # noqa: BLE001
            res.check("runner_exception", False, f"{type(exc).__name__}: {exc}")
        results.append(res)
        print(f"{res.verdict:17} {o['path'].split('/')[-1][:34]:34} {sum(c['ok'] for c in res.checks)}/{len(res.checks)} checks")
    if args.skip_zip:
        zres = skipped_zip_result()
        zcl = None
    else:
        zres, zcl = run_archives(results)
    results.append(zres)
    print(f"{zres.verdict:27} {'ZIP import + hostile + duplicates':34} {sum(c['ok'] for c in zres.checks)}/{len(zres.checks)} checks")
    # Audit chain integrity only applies when the ZIP tenant was created by this run.
    if not args.skip_zip:
        chain = subprocess.run([PY, "-I", "-c", f"""
import sys,json; sys.path.insert(0,'{ROOT}')
from uuid import UUID
from sqlalchemy import create_engine
from packages.api.db_context import api_tenant_transaction
from packages.api.auth import AuthenticatedIdentity
from packages.audit.chain import verify_chain
import os
e=create_engine('postgresql+psycopg://channel360_test_app@127.0.0.1:5432/{ISO_DB}')
ident=AuthenticatedIdentity(subject='x',tenant_id=UUID('{zcl.tok['tenant_id']}'),role='OWNER')
with e.connect() as c, api_tenant_transaction(c, ident):
    print(json.dumps(verify_chain(c, ident.tenant_id)))
"""], capture_output=True, text=True, env={**os.environ, "PGPASSFILE": str(ISO_PGPASS)})
        zres.check("audit hash chain verifies for the tenant", chain.stdout.strip().startswith("[true"), (chain.stdout + chain.stderr).strip()[:100])
    # ---- write results
    json.dump([{"file": r.name, "kind": r.kind, "verdict": r.verdict, "restriction": r.restriction, "timings": r.timings, "checks": r.checks} for r in results],
              open(OUT / "ACCEPTANCE_RESULTS.json", "w"), indent=1)
    lines = ["# Stakeholder acceptance results (private, generated by scripts/corpus/acceptance_run.py)", "",
             f"Run: {time.strftime('%Y-%m-%d %H:%M')} · API {API} · fresh isolated tenant per document", "",
             "| File | Verdict | Checks | Timings (s) |", "|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r.name} | **{r.verdict}** | {sum(c['ok'] for c in r.checks)}/{len(r.checks)} | {r.timings or ''} |")
    lines.append("")
    for r in results:
        lines.append(f"## {r.name} — {r.verdict}")
        if r.restriction:
            lines.append(f"*Safe restriction:* {r.restriction}")
        for c in r.checks:
            lines.append(f"- {'PASS' if c['ok'] else '**FAIL**'} `{c['check']}`" + (f" — {c['detail']}" if c["detail"] and not c["ok"] else ""))
        lines.append("")
    (OUT / "ACCEPTANCE_RESULTS.md").write_text("\n".join(lines))
    tally = {v: sum(1 for r in results if r.verdict == v) for v in ("PASS", "SAFE RESTRICTION", "FAIL", "SKIPPED — OUT OF SCOPE")}
    print("TOTAL", tally)
    return 1 if tally["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
