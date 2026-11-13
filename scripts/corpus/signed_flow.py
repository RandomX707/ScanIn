"""Live Feature-D check on the ISOLATED stack: signed stakeholder PDFs -> truthful status -> unsigned working copy -> edits."""
from __future__ import annotations
import hashlib, io, json, os, sys, urllib.error, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from iso_client import API, Client, token  # noqa: E402
import pypdf  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]; CORPUS = ROOT / "Policy Copies"
OUT = Path(os.environ.get("CHANNEL360_RESULTS_DIR") or (ROOT / ".private/feature-results"))
results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail))); print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else "")); return ok
MARKER = "DRAFT — MODIFIED COPY — NOT OFFICIAL"
EXPECTED = {"Health Policy copy.pdf": "integrity_valid_trust_not_established", "Motor  Policy.pdf": "integrity_valid_trust_not_established", "Marine Policy copy.pdf": "malformed"}

def raw_status(path, tok, method="POST", body=b'{"acknowledge":true}'):
    req = urllib.request.Request(API + path, data=body, method=method, headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"} if tok else {"Content-Type": "application/json"})
    try: return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e: return e.code

def flow(name):
    c = Client(); src = CORPUS / name; data = src.read_bytes(); sha = hashlib.sha256(data).hexdigest(); tag = name.split()[0]
    s, reg, insp = c.upload(src); d, v1 = reg["document_id"], reg["version_id"]
    pv = c.json(f"/api/documents/{d}/versions/{v1}/preview"); sig = pv["signature"]
    check(f"{tag}: signature state is truthful ({EXPECTED[name]})", sig["state"] == EXPECTED[name], sig["state"])
    check(f"{tag}: never labelled validated/trusted without a trust policy", sig["validated"] is False and sig["trust_policy_configured"] is False, sig["label"])
    check(f"{tag}: editing the signed original is blocked", pv["edit"]["mode"] == "none" and pv["working_copy"]["available"], pv["edit"]["reason"][:60])
    s, body, _ = c.post(f"/api/documents/{d}/versions/{v1}/save", {"pdf_edits": [{"id": "p1:page:1-2", "old": "x", "new": "y"}]})
    check(f"{tag}: direct save on the signed original is refused (409 pdf_signed)", s == 409 and body.get("code") == "pdf_signed", f"{s} {body.get('code')}")
    # authorization / validation
    s, b, _ = c.post(f"/api/documents/{d}/versions/{v1}/working-copy", {}); check(f"{tag}: acknowledgement is required", s == 422 and b["code"] == "acknowledgement_required", f"{s} {b.get('code')}")
    check(f"{tag}: non-owner role rejected", raw_status(f"/api/documents/{d}/versions/{v1}/working-copy", token(c.tenant, role="VIEWER")) in (401, 403), "")
    check(f"{tag}: no token rejected", raw_status(f"/api/documents/{d}/versions/{v1}/working-copy", None) == 401, "")
    other = Client(); s, _, _ = other.post(f"/api/documents/{d}/versions/{v1}/working-copy", {"acknowledge": True}); check(f"{tag}: cross-tenant request is 404", s == 404, s)
    # create
    s, body, _ = c.post(f"/api/documents/{d}/versions/{v1}/working-copy", {"acknowledge": True})
    if not check(f"{tag}: working copy created", s == 200 and body["classification"] == "unsigned_working_copy" and body["outcome"] == "VERIFIED", f"{s} {body.get('code') or body.get('outcome')}"): return
    v2 = body["version_id"]
    s, f2, _ = c.file(d, v2); rd = pypdf.PdfReader(io.BytesIO(f2))
    flat = f2.replace(b"\n", b"").replace(b"\r", b"")
    check(f"{tag}: working copy reopens as a genuine PDF with the same page count", f2[:5] == b"%PDF-" and len(rd.pages) == len(pypdf.PdfReader(io.BytesIO(data), strict=False).pages), len(rd.pages))
    check(f"{tag}: working copy has NO signature structures at all", not any(m in flat for m in (b"/ByteRange", b"/DocMDP", b"/SigFlags", b"/Type/Sig", b"/Type /Sig", b"/FT/Sig", b"/FT /Sig")), "")
    check(f"{tag}: visible marker on every page", all(MARKER.replace(" ", "") in (p.extract_text() or "").replace(" ", "") for p in rd.pages), "")
    pv2 = c.json(f"/api/documents/{d}/versions/{v2}/preview")
    check(f"{tag}: working copy reports no signature and is classified unsigned_working_copy", pv2["signature"]["state"] == "none" and pv2["classification"] == "unsigned_working_copy", f"{pv2['signature']['state']} {pv2['classification']}")
    check(f"{tag}: working copy is editable (pdf_text)", pv2["edit"]["mode"] == "pdf_text", pv2["edit"]["mode"])
    s, b, _ = c.post(f"/api/documents/{d}/versions/{v2}/working-copy", {"acknowledge": True}); check(f"{tag}: cannot make a working copy of a working copy", s == 409 and b["code"] == "already_working_copy", f"{s} {b.get('code')}")
    check(f"{tag}: signed original byte-identical", hashlib.sha256(c.file(d, v1)[1]).hexdigest() == sha, "")
    fold = next(f for f in c.json("/api/folders")["items"] if f["document_id"] == d)
    check(f"{tag}: History marks v2 as an unsigned working copy with parent v1", fold["versions"][1]["classification"] == "unsigned_working_copy" and fold["versions"][1]["parent_number"] == 1, fold["versions"][1]["classification"])
    audit = c.json(f"/api/documents/{d}/audit")["items"]
    check(f"{tag}: audit records the working-copy action", any((e["payload"] or {}).get("unsigned_working_copy") and (e["payload"] or {}).get("version_id") == v2 for e in audit), "")
    # edit the working copy
    pages_text = None
    for page in range(1, 4):
        page_runs = [r for r in c.json(f"/api/documents/{d}/versions/{v2}/pdf-text?page={page}")["runs"] if r["e"] and r["size"] >= 6 and 6 <= len(r["text"]) <= 40 and r["text"].replace(" ", "").isalpha()]
        runs = [r for r in page_runs if " " in r["text"]] or page_runs           # some PDFs store one word per text object
        if runs: break
    check(f"{tag}: the working copy has editable text to change", bool(runs), f"{len(runs)} candidate runs")
    notice = [r for r in c.json(f"/api/documents/{d}/versions/{v2}/pdf-text?page=1")["runs"] if "NOT OFFICIAL" in r["text"] or "Unsigned working copy of" in r["text"]]
    check(f"{tag}: the notice strip is shown locked in the editor", bool(notice) and all(not r["e"] for r in notice), f"{len(notice)} notice runs")
    if notice:
        s, b, _ = c.post(f"/api/documents/{d}/versions/{v2}/save", {"pdf_edits": [{"id": notice[0]["id"], "old": notice[0]["text"], "new": "x"}]})
        check(f"{tag}: editing/removing the notice is refused", s in (409, 422) and b.get("code") in {"working_copy_notice_locked", "not_editable", "run_not_editable"} or s in (409, 422), f"{s} {b.get('code')}")
    if runs:
        r = runs[0]; new = " ".join(r["text"].split(" ")[:-1]) if " " in r["text"] else r["text"][:-1]
        s, b, _ = c.post(f"/api/documents/{d}/versions/{v2}/save", {"pdf_edits": [{"id": r["id"], "old": r["text"], "new": new}]})
        if check(f"{tag}: edit of the working copy saved as v3 (VERIFIED)", s == 200 and b["outcome"] == "VERIFIED", f"{s} {b.get('code') or b.get('outcome')}"):
            v3 = b["version_id"]; f3 = c.file(d, v3)[1]; rd3 = pypdf.PdfReader(io.BytesIO(f3))
            check(f"{tag}: v3 reopens, keeps the marker and has no signature", all(MARKER.replace(" ", "") in (p.extract_text() or "").replace(" ", "") for p in rd3.pages) and b"/ByteRange" not in f3, "")
            fold = next(f for f in c.json("/api/folders")["items"] if f["document_id"] == d)
            check(f"{tag}: v3 inherits working-copy classification, parent v2", fold["versions"][2]["classification"] == "unsigned_working_copy" and fold["versions"][2]["parent_number"] == 2, "")
            s, cmp, _ = c.call("GET", f"/api/documents/{d}/compare?base={v1}&target={v3}")
            check(f"{tag}: compare signed original vs edited working copy", s == 200 and cmp["relation"]["path_numbers"] == [1, 2, 3] and cmp["signature"]["base"]["status"] != "none" and cmp["signature"]["target"]["status"] == "none", f"{s} {cmp.get('summary')}")
            check(f"{tag}: compare reports working-copy classification, never the signature as retained", cmp["target"]["classification"] == "unsigned_working_copy" and cmp["base"]["classification"] == "original", cmp["target"]["classification"])
            check(f"{tag}: signed original still byte-identical after edits", hashlib.sha256(c.file(d, v1)[1]).hexdigest() == sha, "")

if __name__ == "__main__":
    for n in EXPECTED: flow(n)
    c = Client(); s, reg, _ = c.upload(CORPUS / "CAR Policy copy.pdf"); s, b, _ = c.post(f"/api/documents/{reg['document_id']}/versions/{reg['version_id']}/working-copy", {"acknowledge": True})
    check("unsigned PDF: working copy not offered (409 not_signed)", s == 409 and b["code"] == "not_signed", f"{s} {b.get('code')}")
    failed = [r for r in results if not r[1]]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "signed.json").write_text(json.dumps([{"check": n, "pass": ok} for n, ok, _ in results], indent=1))
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed"); sys.exit(1 if failed else 0)
