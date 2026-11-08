from __future__ import annotations

import io
import hashlib

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from packages.core.adapters.pdf.adapter import PdfAdapter, PdfDocumentError
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import build_patch_plan
from packages.core.verification.pdf import PdfIndependentVerifier
from packages.workflow.release import NativeReleaseWorkflow


def _form_pdf(*, value: str = "Ada", maxlen: int = 20, width: int = 180) -> bytes:
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(32, 760, "Synthetic insurance form")
    pdf.acroForm.textfield(name="claimant", x=40, y=700, width=width, height=24,
                           value=value, maxlen=maxlen)
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def test_pdf_edit_is_incremental_and_preserves_unrelated_form_and_pixels():
    source = _form_pdf()
    adapter = PdfAdapter()
    target = adapter.targets(source)[0]
    patch = Patch(__import__("hashlib").sha256(source).hexdigest(), target.id, target.value,
                  target.fingerprint, TypedValue("string", "Adele"))
    plan = build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "synthetic-edit",
                                                                  "actor_id": "synthetic-processor"})
    candidate = adapter.apply(source, plan)
    assert candidate.startswith(source)
    assert PdfReader(io.BytesIO(candidate)).get_fields()["claimant"]["/V"] == "Adele"
    report = PdfIndependentVerifier().verify(source, candidate, plan)
    assert report.outcome == "VERIFIED", report.as_dict()


def test_pdf_preflight_rejects_maxlen_and_widget_overflow():
    source = _form_pdf(maxlen=4)
    adapter = PdfAdapter()
    target = adapter.targets(source)[0]
    digest = __import__("hashlib").sha256(source).hexdigest()
    too_long = Patch(digest, target.id, target.value, target.fingerprint, TypedValue("string", "longer"))
    with pytest.raises(PdfDocumentError, match="max_length_exceeded"):
        adapter.preflight(source, target, too_long)

    narrow_source = _form_pdf(maxlen=40, width=60)
    narrow_target = adapter.targets(narrow_source)[0]
    overflow = Patch(hashlib.sha256(narrow_source).hexdigest(), narrow_target.id, narrow_target.value,
                     narrow_target.fingerprint, TypedValue("string", "A value too wide"))
    with pytest.raises(PdfDocumentError, match="widget_overflow"):
        adapter.preflight(narrow_source, narrow_target, overflow)


def test_pdf_checkbox_radio_and_dropdown_updates_use_real_appearance_options():
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    form = pdf.acroForm
    form.checkbox(name="consent", x=20, y=700, checked=False)
    form.radio(name="coverage", value="basic", x=20, y=660, selected=True)
    form.radio(name="coverage", value="extended", x=50, y=660)
    form.choice(name="region", x=20, y=600, value="CA", options=["CA", "NY"])
    pdf.showPage(); pdf.save()
    source = output.getvalue()
    adapter = PdfAdapter()
    by_name = {target.id: target for target in adapter.targets(source)}
    from packages.core.models.contracts import Patch
    values = {"pdf:consent": TypedValue("boolean", True), "pdf:coverage": TypedValue("choice", "extended"),
              "pdf:region": TypedValue("choice", "NY")}
    patches = [Patch(hashlib.sha256(source).hexdigest(), target_id, by_name[target_id].value,
                     by_name[target_id].fingerprint, value) for target_id, value in values.items()]
    plan = build_patch_plan(adapter, source, patches,
                            provenance={"edit_set_id": "synthetic-pdf-choice", "actor_id": "synthetic-processor"})
    candidate = adapter.apply(source, plan)
    assert candidate.startswith(source)
    report = PdfIndependentVerifier().verify(source, candidate, plan)
    assert report.outcome == "VERIFIED", report.as_dict()


def test_pdf_corrupt_and_encrypted_documents_fail_closed():
    with pytest.raises(PdfDocumentError, match="invalid_pdf"):
        PdfAdapter().inspect(b"%PDF-corrupt")
    source = _form_pdf()
    reader = PdfReader(io.BytesIO(source))
    from pypdf import PdfWriter
    writer = PdfWriter(clone_from=reader)
    writer.encrypt("synthetic-password")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    with pytest.raises(PdfDocumentError, match="encrypted_pdf"):
        PdfAdapter().inspect(encrypted.getvalue())


@pytest.mark.parametrize("restriction", ["xfa", "certified", "signed"])
def test_pdf_xfa_certified_and_signed_sources_are_read_only(restriction: str):
    from pypdf import PdfWriter
    from pypdf.generic import (ArrayObject, ByteStringObject, DictionaryObject, NameObject,
                               NumberObject, TextStringObject)
    reader = PdfReader(io.BytesIO(_form_pdf()))
    writer = PdfWriter(clone_from=reader)
    if restriction == "xfa":
        writer._root_object["/AcroForm"].get_object()[NameObject("/XFA")] = TextStringObject("synthetic-xfa")
    elif restriction == "certified":
        perms = DictionaryObject({NameObject("/DocMDP"): writer._root_object.indirect_reference})
        writer._root_object[NameObject("/Perms")] = writer._add_object(perms)
    else:
        signature = DictionaryObject({
            NameObject("/FT"): NameObject("/Sig"), NameObject("/T"): TextStringObject("synthetic_signature"),
            NameObject("/V"): DictionaryObject({NameObject("/Type"): NameObject("/Sig"),
                                                  NameObject("/ByteRange"): ArrayObject([NumberObject(0), NumberObject(1),
                                                                                         NumberObject(2), NumberObject(3)]),
                                                  NameObject("/Contents"): ByteStringObject(b"synthetic")}),
        })
        writer._root_object["/AcroForm"].get_object()["/Fields"].append(writer._add_object(signature))
    output = io.BytesIO(); writer.write(output)
    source = output.getvalue()
    targets = PdfAdapter().targets(source)
    assert targets and all(not target.editable for target in targets)
    assert PdfAdapter().inspect(source).level == "read_only"
