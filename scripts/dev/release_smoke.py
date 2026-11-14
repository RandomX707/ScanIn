"""Release smoke test on SYNTHETIC documents only (no stakeholder data), through the real API of an ISOLATED stack (channel360_iso_* DB).
Exercises the actual release configuration: PDF text edit end to end, DOCX sandboxed render + native save, XLSX native save with formula
preservation, signed-PDF working-copy flow (synthetic structurally-signed PDF), sandbox status. Prints verdicts and counts only.

  CHANNEL360_API=http://127.0.0.1:8010 C360_ISO_DB=channel360_iso_xxx python scripts/dev/release_smoke.py
The CLIENT side needs python-docx (to author the synthetic DOCX); the server under test does not."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from iso_client import Client  # noqa: E402

import docx  # noqa: E402
import openpyxl  # noqa: E402
import pypdf  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    return bool(ok)


def synthetic_pdf() -> bytes:
    buffer = io.BytesIO()
    page = canvas.Canvas(buffer, pagesize=(595, 842))
    page.setFont("Helvetica", 12)
    for i, line in enumerate(["Synthetic schedule of premium for release smoke", "Policy holder name is Example Person Limited",
                              "Sum insured is one hundred thousand units", "Issued for testing purposes only"]):
        page.drawString(72, 760 - 30 * i, line)
    page.showPage()
    page.save()
    return buffer.getvalue()


def fake_signed(data: bytes) -> bytes:
    spec = importlib.util.spec_from_file_location("wc_tests", ROOT / "tests/unit/test_pdf_working_copy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.add_fake_signature(data)


def upload(c: Client, name: str, data: bytes):
    s, reg, _ = c.upload_bytes(name, data) if hasattr(c, "upload_bytes") else c.call("PUT", f"/api/documents?filename={name}", data, {"Content-Type": "application/octet-stream"})
    c.call("POST", f"/api/documents/{reg['document_id']}/inspect")
    return s, reg["document_id"], reg["version_id"]


def main() -> int:
    c = Client()
    s, st, _ = c.call("GET", "/health/sandbox")
    check("sandbox runtime self-test verified on this host", s == 200 and st["verified_here"] and st["tools_present"]["soffice"] and st["tools_present"]["openssl"], st.get("detail", ""))

    # ---- PDF text editing end to end
    pdf = synthetic_pdf(); sha = hashlib.sha256(pdf).hexdigest()
    s, d, v = upload(c, "synthetic.pdf", pdf); check("PDF imported", s == 200)
    pv = c.json(f"/api/documents/{d}/versions/{v}/preview")
    check("PDF is editable as text", pv["edit"]["mode"] == "pdf_text", pv["edit"]["mode"])
    runs = [r for r in c.json(f"/api/documents/{d}/versions/{v}/pdf-text?page=1")["runs"] if r["e"] and "Example Person" in r["text"]]
    check("target text run found", len(runs) == 1, str(len(runs)))
    run = runs[0]; edit = {"id": run["id"], "old": run["text"], "new": run["text"].replace("Example Person Limited", "Sample Holder Limited")}
    s, b, h = c.call("POST", f"/api/documents/{d}/versions/{v}/pdf-edit/preview", __import__("json").dumps({"pdf_edits": [edit]}).encode(), {"Content-Type": "application/json"}, raw=True)
    check("edit preview is a PDF", s == 200 and b[:5] == b"%PDF-", str(s))
    s, res, _ = c.post(f"/api/documents/{d}/versions/{v}/save", {"pdf_edits": [edit]})
    check("PDF save verified", s == 200 and res.get("outcome") in ("VERIFIED", "SAVED"), f"{s} {res.get('outcome') or res.get('code')}")
    new = c.file(d, res["version_id"])[1] if s == 200 else b""
    text = "".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(new)).pages) if new else ""
    check("new PDF contains the edit and not the old text", "Sample Holder Limited" in text and "Example Person Limited" not in text)
    check("original version byte-identical", hashlib.sha256(c.file(d, v)[1]).hexdigest() == sha)
    bad = {"id": run["id"], "old": run["text"], "new": run["text"] + " ₹₹₹₹"}
    s, b, _ = c.post(f"/api/documents/{d}/versions/{v}/save", {"pdf_edits": [bad]})
    check("an unsupported edit is refused, not silently saved", s in (409, 422), f"{s} {b.get('code')}")

    # ---- DOCX sandboxed render + native save
    document = docx.Document(); document.add_heading("Synthetic release smoke", 1)
    document.add_paragraph("The quoted annual premium for the synthetic account is payable in advance.")
    table = document.add_table(rows=2, cols=2); table.cell(0, 0).text = "Item"; table.cell(0, 1).text = "Amount"; table.cell(1, 0).text = "Premium"; table.cell(1, 1).text = "1000"
    buffer = io.BytesIO(); document.save(buffer); dx = buffer.getvalue(); dsha = hashlib.sha256(dx).hexdigest()
    s, dd, dv = upload(c, "synthetic.docx", dx); check("DOCX imported", s == 200)
    s, pdf_b, hdr = c.call("GET", f"/api/documents/{dd}/versions/{dv}/render", raw=True)
    check("DOCX renders through the sandboxed LibreOffice", s == 200 and pdf_b[:5] == b"%PDF-" and hdr.get("x-render-engine") == "libreoffice-sandboxed", str(s))
    pvd = c.json(f"/api/documents/{dd}/versions/{dv}/preview")
    blocks = [x for x in pvd.get("blocks", []) if x["e"] and "quoted annual premium" in x["text"]]
    check("DOCX paragraph is editable", len(blocks) == 1, str(len(blocks)))
    if blocks:
        s, res, _ = c.post(f"/api/documents/{dd}/versions/{dv}/save", {"changes": [{"target_id": blocks[0]["target_id"], "fingerprint": blocks[0]["fp"], "value": blocks[0]["text"] + " Amended."}]})
        check("DOCX native save verified", s == 200 and res.get("outcome") in ("VERIFIED", "STRUCTURALLY_VERIFIED", "SAVED"), f"{s} {res.get('outcome') or res.get('code')}")
        new = c.file(dd, res["version_id"])[1] if s == 200 else b""
        reopened = docx.Document(io.BytesIO(new)) if new else None
        check("saved DOCX reopens with the edit and the table intact", bool(reopened) and any("Amended." in p.text for p in reopened.paragraphs) and len(reopened.tables) == 1)
    check("original DOCX byte-identical", hashlib.sha256(c.file(dd, dv)[1]).hexdigest() == dsha)

    # ---- XLSX native save, formulas preserved
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Data"; ws["A1"] = "Label"; ws["B1"] = "Amount"; ws["A2"] = "Alpha"; ws["B2"] = 10; ws["A3"] = "Beta"; ws["B3"] = 20; ws["B4"] = "=SUM(B2:B3)"
    buffer = io.BytesIO(); wb.save(buffer); xb = buffer.getvalue(); xsha = hashlib.sha256(xb).hexdigest()
    s, xd, xv = upload(c, "synthetic.xlsx", xb); check("XLSX imported", s == 200)
    pvx = c.json(f"/api/documents/{xd}/versions/{xv}/preview"); cells = [x for sh in pvx["sheets"] for row in sh["rows"] for x in row if x]
    target = next((x for x in cells if x["e"] and x["v"] == "Alpha"), None)
    check("XLSX text cell is editable", target is not None)
    if target:
        s, res, _ = c.post(f"/api/documents/{xd}/versions/{xv}/save", {"changes": [{"target_id": target["id"], "fingerprint": target["fp"], "value": "Alpha edited"}]})
        check("XLSX native save verified", s == 200 and res.get("outcome") in ("VERIFIED", "SAVED"), f"{s} {res.get('outcome') or res.get('code')}")
        new = c.file(xd, res["version_id"])[1] if s == 200 else b""
        reopened = openpyxl.load_workbook(io.BytesIO(new)) if new else None
        check("saved XLSX reopens with the edit and the formula intact", bool(reopened) and reopened["Data"]["A2"].value == "Alpha edited" and reopened["Data"]["B4"].value == "=SUM(B2:B3)")
    check("original XLSX byte-identical", hashlib.sha256(c.file(xd, xv)[1]).hexdigest() == xsha)

    # ---- signed-PDF working copy (synthetic structurally signed PDF)
    signed = fake_signed(synthetic_pdf()); ssha = hashlib.sha256(signed).hexdigest()
    s, sd, sv = upload(c, "synthetic-signed.pdf", signed); check("signed-structure PDF imported", s == 200)
    pvs = c.json(f"/api/documents/{sd}/versions/{sv}/preview")
    check("signature is reported, never presented as an official valid signature", (pvs.get("signature") or {}).get("state") not in (None, "none"), str((pvs.get("signature") or {}).get("state")))
    s, b, _ = c.post(f"/api/documents/{sd}/versions/{sv}/working-copy", {})
    check("working copy needs explicit acknowledgement", s == 422 and b.get("code") == "acknowledgement_required", f"{s} {b.get('code')}")
    s, wcopy, _ = c.post(f"/api/documents/{sd}/versions/{sv}/working-copy", {"acknowledge": True})
    check("unsigned working copy created and verified", s == 200 and wcopy.get("classification") == "unsigned_working_copy" and wcopy.get("outcome") == "VERIFIED", f"{s} {wcopy.get('code') or wcopy.get('outcome')}")
    if s == 200:
        data = c.file(sd, wcopy["version_id"])[1]; flat = data.replace(b"\n", b"")
        check("working copy has no signature structures", not any(m in flat for m in (b"/ByteRange", b"/DocMDP", b"/SigFlags", b"/Type/Sig", b"/FT/Sig")))
        check("every page carries the NOT OFFICIAL marking", all("NOTOFFICIAL" in (p.extract_text() or "").replace(" ", "") for p in pypdf.PdfReader(io.BytesIO(data)).pages))
    check("signed original byte-identical", hashlib.sha256(c.file(sd, sv)[1]).hexdigest() == ssha)
    other = Client(); s, _, _ = other.call("GET", f"/api/documents/{d}/history")
    check("another tenant cannot read these documents (404)", s == 404, str(s))

    failed = results.count(False)
    print(f"{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
