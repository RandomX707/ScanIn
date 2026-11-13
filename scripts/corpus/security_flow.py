"""Security checks for the new endpoints on the ISOLATED stack. Prints counts/status codes only."""
from __future__ import annotations
import hashlib, io, json, os, re, sys, urllib.error, urllib.request, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from iso_client import API, Client, token  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]; CORPUS = ROOT / "Policy Copies"
OUT = Path(os.environ.get("CHANNEL360_RESULTS_DIR") or (ROOT / ".private/feature-results"))
results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail))); print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else "")); return ok
def raw(method, path, tok, body=None):
    req = urllib.request.Request(API + path, data=body, method=method, headers=({"Authorization": f"Bearer {tok}"} if tok else {}) | ({"Content-Type": "application/json"} if body else {}))
    try: return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e: return e.code

def main():
    a, b = Client(), Client()
    s, reg, _ = a.upload(CORPUS / "QCR/Health Quote Format.docx"); dd, vd = reg["document_id"], reg["version_id"]
    s, reg, _ = a.upload(CORPUS / "Health Policy copy.pdf"); dp, vp = reg["document_id"], reg["version_id"]
    s, body, _ = a.post(f"/api/documents/{dp}/versions/{vp}/working-copy", {"acknowledge": True}); vp2 = body["version_id"]
    endpoints = [("GET", f"/api/documents/{dd}/versions/{vd}/render", None), ("GET", f"/api/documents/{dp}/compare?base={vp}&target={vp2}", None),
                 ("GET", f"/api/documents/{dp}/versions/{vp2}/changes", None), ("GET", f"/api/documents/{dp}/history", None),
                 ("POST", f"/api/documents/{dp}/versions/{vp}/working-copy", b'{"acknowledge":true}')]
    for method, path, body_ in endpoints:
        short = path.split("/api/documents/")[1].split("?")[0].split("/", 1)[1] if path.count("/") > 3 else "history"
        check(f"{method} …/{short}: unauthenticated -> 401", raw(method, path, None, body_) == 401, raw(method, path, None, body_))
        check(f"{method} …/{short}: non-owner role -> 401/403", raw(method, path, token(a.tenant, role="VIEWER"), body_) in (401, 403), "")
        s2 = raw(method, path, b.tok, body_); check(f"{method} …/{short}: another tenant -> 404 (no existence leak)", s2 == 404, s2)
    # nothing leaks storage paths or other tenants' ids
    s, cmp, _ = a.call("GET", f"/api/documents/{dp}/compare?base={vp}&target={vp2}"); s, hist, _ = a.call("GET", f"/api/documents/{dp}/history")
    blob = json.dumps([cmp, hist])
    check("responses contain no storage keys or filesystem paths", not re.search(r"(/Users/|/private/|\.runtime|storage_key|document-storage|iso-stack)", blob), "")
    check("responses never contain the other tenant's id", b.tenant not in blob, "")
    # tamper detection: flip one byte inside the signed byte range of the Health PDF
    data = bytearray((CORPUS / "Health Policy copy.pdf").read_bytes()); m = re.search(rb"/ByteRange\s*\[\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*\]", bytes(data)); signed_end = int(m.group(2))
    idx = bytes(data).find(b"/Producer", 0, signed_end)
    pos = (bytes(data).find(b"(", idx) + 2) if idx > 0 else signed_end - 50
    data[pos] = data[pos] ^ 0x01
    tmp = Path("/private/tmp") / "c360-tampered.pdf"; tmp.write_bytes(bytes(data))
    try:
        s, reg, insp = a.upload(tmp)
        if s == 200:
            pv = a.json(f"/api/documents/{reg['document_id']}/versions/{reg['version_id']}/preview")
            check("a signed PDF altered after signing is reported as 'does not verify', never intact", pv["signature"]["state"] in ("integrity_invalid", "malformed") and pv["signature"]["validated"] is False, pv["signature"]["state"])
            check("the altered file is still not editable in place", pv["edit"]["mode"] == "none", pv["edit"]["mode"])
        else:
            check("tampered PDF handled safely at import", s in (200, 422), s)
    finally:
        tmp.unlink(missing_ok=True)
    # a hand-made fake: valid-looking signature container text copied into an unrelated PDF
    fake = (CORPUS / "CAR Policy copy.pdf").read_bytes() + b"\n% fake\n1 0 obj<</Type/Sig/Filter/Adobe.PPKLite/SubFilter/adbe.pkcs7.detached/ByteRange[0 100 200 50]/Contents<" + b"30" + b"00" * 40 + b">>>endobj\n"
    tmp2 = Path("/private/tmp") / "c360-fake-sig.pdf"; tmp2.write_bytes(fake)
    try:
        s, reg, insp = a.upload(tmp2)
        if s == 200:
            pv = a.json(f"/api/documents/{reg['document_id']}/versions/{reg['version_id']}/preview")
            check("a fabricated signature object is never reported valid or trusted", pv["signature"]["validated"] is False and pv["signature"]["state"] in ("malformed", "integrity_invalid", "not_validated"), pv["signature"]["state"])
        else:
            check("fabricated signature file handled safely at import", s in (200, 422), s)
    finally:
        tmp2.unlink(missing_ok=True)
    # malformed / hostile DOCX
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z: z.writestr("[Content_Types].xml", "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>"); z.writestr("junk.txt", "x")
    tmp3 = Path("/private/tmp") / "c360-malformed.docx"; tmp3.write_bytes(out.getvalue())
    try:
        s, reg, insp = a.upload(tmp3)
        if s == 200 and reg:
            code = a.call("GET", f"/api/documents/{reg['document_id']}/versions/{reg['version_id']}/render", raw=True)[0]
            check("malformed DOCX never yields a 5xx from the render endpoint", code < 500, code)
        else:
            check("malformed DOCX is refused at inspection (not a server error)", s < 500, s)
    finally:
        tmp3.unlink(missing_ok=True)
    # unsupported types
    s, reg, _ = a.upload(CORPUS / "CAR Policy copy.pdf")
    check("render of a PDF is refused cleanly (415, not a crash)", a.call("GET", f"/api/documents/{reg['document_id']}/versions/{reg['version_id']}/render", raw=True)[0] == 415, "")
    check("compare of unrelated version ids is a clean 404", a.call("GET", f"/api/documents/{reg['document_id']}/compare?base={vd}&target={vd}")[0] == 404, "")
    failed = [r for r in results if not r[1]]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "security.json").write_text(json.dumps([{"check": n, "pass": ok} for n, ok, _ in results], indent=1))
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed"); sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
