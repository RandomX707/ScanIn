"""Unsigned working copy: no signature survives, a notice is on every page, original content is preserved, source untouched."""
from __future__ import annotations

import hashlib
import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject, ByteStringObject
from reportlab.pdfgen import canvas

from packages.core import pdf_signature, pdf_working_copy as wc


def make_pdf(pages: int = 3) -> bytes:
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=(595, 842))
    for i in range(pages):
        c.setFont("Helvetica", 12); c.drawString(72, 700, f"Policy clause {i + 1}: cover applies to the insured premises only.")
        c.drawString(72, 680, "Schedule of benefits and limits follows."); c.showPage()
    c.save(); return buf.getvalue()


def add_fake_signature(pdf: bytes) -> bytes:
    """Add a signature field + signature value + DocMDP permission + SigFlags the way a signer would."""
    reader = PdfReader(io.BytesIO(pdf)); writer = PdfWriter(clone_from=reader)
    sig = DictionaryObject({NameObject("/Type"): NameObject("/Sig"), NameObject("/Filter"): NameObject("/Adobe.PPKLite"),
                            NameObject("/SubFilter"): NameObject("/adbe.pkcs7.detached"),
                            NameObject("/ByteRange"): ArrayObject([NumberObject(0), NumberObject(10), NumberObject(20), NumberObject(30)]),
                            NameObject("/Contents"): ByteStringObject(b"\x30\x03\x02\x01\x01")})
    sig_ref = writer._add_object(sig)  # noqa: SLF001
    widget = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"), NameObject("/FT"): NameObject("/Sig"),
                               NameObject("/T"): TextStringObject("Signature1"), NameObject("/V"): sig_ref,
                               NameObject("/Rect"): ArrayObject([NumberObject(50), NumberObject(50), NumberObject(200), NumberObject(100)])})
    widget_ref = writer._add_object(widget)  # noqa: SLF001
    writer.pages[0][NameObject("/Annots")] = ArrayObject([widget_ref])
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): ArrayObject([widget_ref]), NameObject("/SigFlags"): NumberObject(3)})  # noqa: SLF001
    writer._root_object[NameObject("/Perms")] = DictionaryObject({NameObject("/DocMDP"): sig_ref})  # noqa: SLF001
    out = io.BytesIO(); writer.write(out); return out.getvalue()


def test_signature_structures_are_removed_everywhere():
    signed = add_fake_signature(make_pdf())
    assert pdf_signature.classify(signed)["status"] != "none"             # the fixture really looks signed
    data, report = wc.build(signed, name="Policy.pdf", source_sha256=hashlib.sha256(signed).hexdigest(), source_state="x")
    assert report["outcome"] == "VERIFIED" and report["signature_artefacts_removed"] >= 1
    assert pdf_signature.classify(data)["status"] == "none"
    flat = data.replace(b"\n", b"").replace(b"\r", b"")
    for marker in (b"/ByteRange", b"/DocMDP", b"/SigFlags", b"/Type/Sig", b"/Type /Sig", b"/FT/Sig", b"/FT /Sig"):
        assert marker not in flat


def test_notice_on_every_page_in_new_space_and_original_text_preserved():
    pdf = make_pdf(3)
    data, report = wc.build(pdf, name="Policy.pdf", source_sha256="a" * 64, source_state="integrity_valid_trust_not_established")
    out, src = PdfReader(io.BytesIO(data)), PdfReader(io.BytesIO(pdf))
    assert len(out.pages) == 3
    for page, original in zip(out.pages, src.pages):
        assert wc.MARKER.replace(" ", "") in page.extract_text().replace(" ", "")
        assert float(page.mediabox.top) == pytest.approx(float(original.mediabox.top) + wc.STRIP_HEIGHT)   # new strip above, content not moved
        assert float(page.mediabox.bottom) == float(original.mediabox.bottom)
        assert original.extract_text().split("\n")[0].strip() in page.extract_text()
    meta = out.metadata
    assert str(meta["/C360WorkingCopy"]) == "true" and str(meta["/C360SourceSHA256"]) == "a" * 64
    assert "not official" in str(meta["/Subject"]).lower()
    assert all(c["ok"] for c in report["checks"])


def test_source_bytes_are_never_modified():
    signed = add_fake_signature(make_pdf())
    before = hashlib.sha256(signed).hexdigest()
    wc.build(signed, name="Policy.pdf", source_sha256=before, source_state="x")
    assert hashlib.sha256(signed).hexdigest() == before


def test_encrypted_and_unreadable_inputs_are_refused():
    with pytest.raises(wc.WorkingCopyRefused):
        wc.build(b"not a pdf", name="x.pdf", source_sha256="0" * 64, source_state="x")
    w = PdfWriter(); w.add_blank_page(100, 100); w.encrypt("secret"); buf = io.BytesIO(); w.write(buf)
    with pytest.raises(wc.WorkingCopyRefused) as refused:
        wc.build(buf.getvalue(), name="x.pdf", source_sha256="0" * 64, source_state="x")
    assert refused.value.code == "working_copy_encrypted"


def test_working_copy_of_a_working_copy_keeps_one_notice_set_and_stays_unsigned():
    once, _ = wc.build(make_pdf(1), name="Policy.pdf", source_sha256="b" * 64, source_state="x")
    twice, report = wc.build(once, name="Policy.pdf", source_sha256="c" * 64, source_state="none")
    assert report["outcome"] == "VERIFIED" and pdf_signature.classify(twice)["status"] == "none"
