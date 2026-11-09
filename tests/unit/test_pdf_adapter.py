from __future__ import annotations

import io
import hashlib
import math
import subprocess
import tempfile
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.colors import black, white
from reportlab.pdfgen import canvas

from packages.core.adapters.pdf.adapter import PdfAdapter, PdfDocumentError
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import build_patch_plan
from packages.core.verification.pdf import PdfIndependentVerifier
from packages.core.verification import pdf as pdf_verification
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


def _complex_two_page_form_pdf() -> bytes:
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    form = pdf.acroForm
    form.textfield(name="target", x=40, y=700, width=210, height=26, value="Ada",
                   borderStyle="solid", borderWidth=1, borderColor=black, fillColor=white,
                   textColor=black, forceBorder=True, fontName="Helvetica", fontSize=12)
    form.textfield(name="untouched", x=40, y=655, width=210, height=26, value="Keep this",
                   borderStyle="solid", borderWidth=1, borderColor=black, fillColor=white,
                   textColor=black, forceBorder=True, fontName="Helvetica", fontSize=12)
    form.checkbox(name="consent", x=40, y=610, checked=True, buttonStyle="check",
                  borderWidth=1, borderColor=black, fillColor=white, forceBorder=True)
    form.radio(name="coverage", value="basic", x=40, y=570, selected=True,
               borderWidth=1, borderColor=black, fillColor=white, forceBorder=True)
    form.radio(name="coverage", value="standard", x=70, y=570,
               borderWidth=1, borderColor=black, fillColor=white, forceBorder=True)
    form.radio(name="coverage", value="premium", x=100, y=570,
               borderWidth=1, borderColor=black, fillColor=white, forceBorder=True)
    form.choice(name="region", x=40, y=520, width=150, height=24, value="CA", options=["CA", "NY", "TX"],
                borderWidth=1, borderColor=black, fillColor=white, forceBorder=True)
    pdf.drawString(32, 760, "Synthetic claim form, page one")
    pdf.showPage()
    form.textfield(name="page_two", x=40, y=700, width=210, height=26, value="Unchanged page",
                   borderStyle="solid", borderWidth=1, borderColor=black, fillColor=white,
                   textColor=black, forceBorder=True, fontName="Helvetica", fontSize=12)
    pdf.drawString(32, 760, "Synthetic claim form, page two")
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
    too_long_patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value,
                           target.fingerprint, TypedValue("string", "longer"))
    from packages.core.planning.planner import PlanError
    with pytest.raises(PlanError, match="max_length_exceeded"):
        build_patch_plan(adapter, source, [too_long_patch], provenance={"edit_set_id": "maxlen-before-freeze"})


@pytest.mark.parametrize(("value", "reason"), [
    ("Café", None),
    ("😀", "unsupported_glyph"),
    ("A", None),
    ("A\nB", "unsupported_control_character"),
])
def test_pdf_text_preflight_uses_font_metrics_and_supported_encoding(value, reason):
    source = _form_pdf(width=100, maxlen=40)
    adapter = PdfAdapter()
    target = adapter.targets(source)[0]
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("string", value))
    if reason:
        with pytest.raises(PdfDocumentError, match=reason):
            adapter.preflight(source, target, patch)
    else:
        adapter.preflight(source, target, patch)


def test_pdf_required_field_empty_is_rejected_before_plan_freeze():
    from pypdf.constants import FieldFlag
    reader = PdfReader(io.BytesIO(_form_pdf()))
    writer = __import__("pypdf").PdfWriter(clone_from=reader)
    writer.update_page_form_field_values(None, {"claimant": "Ada"}, auto_regenerate=False,
                                         flags=FieldFlag.REQUIRED)
    output = io.BytesIO(); writer.write(output)
    source = output.getvalue()
    adapter = PdfAdapter()
    target = adapter.targets(source)[0]
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("string", ""))
    from packages.core.planning.planner import PlanError
    with pytest.raises(PlanError, match="required_field_empty"):
        build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "required-field"})


def test_pdf_preflight_rejects_multiline_when_widget_is_not_multiline():
    source = _form_pdf()
    adapter = PdfAdapter()
    target = adapter.targets(source)[0]
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("string", "line one\nline two"))
    with pytest.raises(PdfDocumentError, match="unsupported_control_character"):
        adapter.preflight(source, target, patch)


def test_pdf_multiline_field_accepts_bounded_supported_lines():
    output = io.BytesIO(); pdf = canvas.Canvas(output)
    pdf.acroForm.textfield(name="notes", x=30, y=680, width=180, height=54, value="old",
                           fieldFlags="multiline", fontName="Helvetica", fontSize=10)
    pdf.showPage(); pdf.save(); source = output.getvalue()
    adapter = PdfAdapter(); target = adapter.targets(source)[0]
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("string", "first line\nsecond line"))
    plan = build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "multiline"})
    candidate = adapter.apply(source, plan)
    assert PdfReader(io.BytesIO(candidate)).get_fields()["notes"]["/V"] == "first line\nsecond line"


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
    assert by_name["pdf:consent"].options == ()
    assert by_name["pdf:consent"].native_locator["on_state"]
    assert set(by_name["pdf:coverage"].options) == {"basic", "extended"}
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


@pytest.mark.parametrize("checked", [True, False])
def test_pdf_radio_and_checkbox_values_are_name_objects_and_widget_states_match(checked):
    from pypdf.generic import NameObject
    from reportlab.pdfgen import canvas

    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    pdf.acroForm.checkbox(name="consent", x=40, y=700, checked=not checked, fieldFlags="",
                          borderWidth=1, forceBorder=True)
    pdf.acroForm.radio(name="coverage", value="basic", x=40, y=650, selected=True, forceBorder=True)
    pdf.acroForm.radio(name="coverage", value="standard", x=75, y=650, forceBorder=True)
    pdf.showPage(); pdf.save()
    source = output.getvalue()
    adapter = PdfAdapter()
    targets = {item.id: item for item in adapter.targets(source)}
    patches = [
        Patch(hashlib.sha256(source).hexdigest(), "pdf:coverage", targets["pdf:coverage"].value,
              targets["pdf:coverage"].fingerprint, TypedValue("choice", "standard")),
        Patch(hashlib.sha256(source).hexdigest(), "pdf:consent", targets["pdf:consent"].value,
              targets["pdf:consent"].fingerprint, TypedValue("boolean", checked)),
    ]
    plan = build_patch_plan(adapter, source, patches, provenance={"edit_set_id": "typed-buttons"})
    candidate = adapter.apply(source, plan)
    reader = PdfReader(io.BytesIO(candidate))
    fields = reader.get_fields()
    assert isinstance(fields["coverage"]["/V"], NameObject)
    assert fields["coverage"]["/V"] == NameObject("/standard")
    assert isinstance(fields["consent"]["/V"], NameObject)
    consent_state = ("/" + targets["pdf:consent"].native_locator["on_state"]) if checked else "/Off"
    assert fields["consent"]["/V"] == NameObject(consent_state)

    radio_widgets = [ref.get_object() for page in reader.pages for ref in page.get("/Annots", [])
                     if ref.get_object().get("/Parent") is not None and
                     ref.get_object()["/Parent"].get_object().get("/T") == "coverage"]
    assert len(radio_widgets) == 2
    assert sum(str(widget["/AS"]) == "/standard" for widget in radio_widgets) == 1
    assert sum(str(widget["/AS"]) == "/Off" for widget in radio_widgets) == 1
    checkbox_widgets = [ref.get_object() for page in reader.pages for ref in page.get("/Annots", [])
                        if ref.get_object().get("/T") == "consent"]
    assert len(checkbox_widgets) == 1
    assert str(checkbox_widgets[0]["/AS"]) == consent_state
    from PIL import ImageChops
    from packages.core.verification.pdf import _render
    assert ImageChops.difference(_render(source, forms_enabled=True)[0],
                                 _render(candidate, forms_enabled=True)[0]).getbbox() is not None
    report = PdfIndependentVerifier().verify(source, candidate, plan)
    assert report.outcome == "VERIFIED", report.as_dict()


def test_pdf_radio_duplicate_export_values_remain_supported():
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    pdf.acroForm.radio(name="coverage", value="standard", x=30, y=700, selected=True, forceBorder=True)
    pdf.acroForm.radio(name="coverage", value="standard", x=65, y=700, forceBorder=True)
    pdf.acroForm.radio(name="coverage", value="premium", x=100, y=700, forceBorder=True)
    pdf.showPage(); pdf.save()
    source = output.getvalue()
    adapter = PdfAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "pdf:coverage")
    assert set(target.options) == {"standard", "premium"}
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("choice", "premium"))
    plan = build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "duplicate-radio-values"})
    candidate = adapter.apply(source, plan)
    reader = PdfReader(io.BytesIO(candidate))
    widgets = [ref.get_object() for ref in reader.pages[0]["/Annots"]
               if ref.get_object().get("/Parent") is not None]
    assert str(reader.get_fields()["coverage"]["/V"]) == "/premium"
    assert [str(widget["/AS"]) for widget in widgets] == ["/Off", "/Off", "/premium"]
    assert PdfIndependentVerifier().verify(source, candidate, plan).outcome == "VERIFIED"


@pytest.mark.parametrize("bad_state", ["wrong_type", "mismatched_as"])
def test_pdf_button_verifier_rejects_typed_or_appearance_state_mismatch(bad_state):
    from pypdf.generic import NameObject, TextStringObject

    source = _complex_two_page_form_pdf()
    adapter = PdfAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "pdf:coverage")
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("choice", "standard"))
    plan = build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "bad-radio-state"})
    candidate = adapter.apply(source, plan)

    def mutate(writer):
        field = next(ref.get_object() for ref in writer._root_object["/AcroForm"].get_object()["/Fields"]
                     if ref.get_object().get("/T") == "coverage")
        if bad_state == "wrong_type":
            field[NameObject("/V")] = TextStringObject("standard")
        else:
            widget = next(widget for widget in _writer_form_widgets(writer, "coverage")
                          if widget.get("/AS") == NameObject("/standard"))
            widget[NameObject("/AS")] = NameObject("/Off")

    tampered = _incremental_tamper(candidate, mutate)
    report = PdfIndependentVerifier().verify(source, tampered, plan)
    assert report.outcome == "BLOCKED"
    assert report.check("intent").status == "FAIL"


def _writer_form_widgets(writer, name):
    return [ref.get_object() for page in writer.pages for ref in page.get("/Annots", [])
            if ref.get_object().get("/Parent") is not None and
            ref.get_object()["/Parent"].get_object().get("/T") == name]


def test_pdf_incremental_edit_verifies_qpdf_object_stream_input():
    source = _form_pdf()
    with tempfile.TemporaryDirectory(prefix="channel360-qpdf-fixture-") as temporary:
        original = Path(temporary) / "reportlab-source.pdf"
        rewritten = Path(temporary) / "qpdf-object-streams.pdf"
        original.write_bytes(source)
        subprocess.run(["qpdf", "--object-streams=generate", str(original), str(rewritten)],
                       check=True, capture_output=True)
        source = rewritten.read_bytes()

    adapter = PdfAdapter()
    target = adapter.targets(source)[0]
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("string", "Adele"))
    plan = build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "qpdf-object-stream-fixture"})
    candidate = adapter.apply(source, plan)
    assert candidate.startswith(source)
    assert PdfIndependentVerifier().verify(source, candidate, plan).outcome == "VERIFIED"


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


def test_form_appearance_edit_changes_only_target_widget_pixels_and_keeps_chrome():
    from PIL import ImageChops
    from packages.core.verification.pdf import _render

    source = _complex_two_page_form_pdf()
    adapter = PdfAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "pdf:target")
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("string", "Alexandra"))
    plan = build_patch_plan(adapter, source, [patch],
                            provenance={"edit_set_id": "synthetic-form-chrome", "actor_id": "synthetic-owner"})
    candidate = adapter.apply(source, plan)

    source_acro = PdfReader(io.BytesIO(source)).root_object["/AcroForm"].get_object()
    candidate_acro = PdfReader(io.BytesIO(candidate)).root_object["/AcroForm"].get_object()
    assert ("/NeedAppearances" in source_acro) == ("/NeedAppearances" in candidate_acro)
    if "/NeedAppearances" in source_acro:
        assert candidate_acro["/NeedAppearances"] == source_acro["/NeedAppearances"]

    before, after = _render(source, forms_enabled=True), _render(candidate, forms_enabled=True)
    assert len(before) == len(after) == 2
    assert ImageChops.difference(before[1], after[1]).getbbox() is None
    bbox = ImageChops.difference(before[0], after[0]).getbbox()
    x0, y0, x1, y1 = bbox
    widget = target.native_locator["widgets"][0]
    wx0, wy0, wx1, wy1 = widget["rect"]
    page_width = float(PdfReader(io.BytesIO(source)).pages[0].mediabox.width)
    page_height = float(PdfReader(io.BytesIO(source)).pages[0].mediabox.height)
    px0, px1 = math.ceil(wx0 * before[0].width / page_width) - 1, math.floor(wx1 * before[0].width / page_width)
    py0 = math.ceil((page_height - wy1) * before[0].height / page_height) - 1
    py1 = math.floor((page_height - wy0) * before[0].height / page_height)
    assert px0 <= x0 and x1 - 1 <= px1
    assert py0 <= y0 and y1 - 1 <= py1

    # The reportlab /MK border is black and the interior background is white.
    field_widget = next(ref.get_object() for ref in PdfReader(io.BytesIO(candidate)).pages[0]["/Annots"]
                        if ref.get_object().get("/T") == "target")
    mk = field_widget["/MK"]
    assert list(mk["/BC"]) == [0, 0, 0]
    assert list(mk["/BG"]) == [1, 1, 1]
    rendered = after[0]
    assert rendered.getpixel((int(wx0), int(rendered.height - wy0 - 1))) != (255, 255, 255)
    assert rendered.getpixel((int(wx1 - 8), int(rendered.height - (wy0 + wy1) / 2))) == (255, 255, 255)
    report = PdfIndependentVerifier().verify(source, candidate, plan)
    assert report.outcome == "VERIFIED", report.as_dict()


def _incremental_tamper(data: bytes, mutate) -> bytes:
    from packages.core.adapters.pdf.adapter import _QpdfConformantIncrementalWriter

    writer = _QpdfConformantIncrementalWriter(PdfReader(io.BytesIO(data)), incremental=True)
    mutate(writer)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("tamper", ["need_appearances", "non_target_appearance"])
def test_pdf_preservation_blocks_global_or_non_target_appearance_changes(tamper: str):
    source = _complex_two_page_form_pdf()
    adapter = PdfAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "pdf:target")
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("string", "Alexandra"))
    plan = build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "synthetic-preservation"})
    candidate = adapter.apply(source, plan)

    def mutate(writer):
        if tamper == "need_appearances":
            from pypdf.generic import BooleanObject, NameObject
            writer._root_object[NameObject("/AcroForm")].get_object()[NameObject("/NeedAppearances")] = BooleanObject(True)
            return
        widget = next(ref.get_object() for ref in writer.pages[0]["/Annots"]
                      if ref.get_object().get("/T") == "untouched")
        appearance = widget["/AP"].get_object()["/N"].get_object()
        appearance.set_data(appearance.get_data() + b"\n% unrelated widget changed\n")

    tampered = _incremental_tamper(candidate, mutate)
    report = PdfIndependentVerifier().verify(source, tampered, plan)
    preservation = next(item for item in report.checks if item.check == "preservation")
    assert report.outcome == "BLOCKED", report.as_dict()
    assert preservation.status == "FAIL", report.as_dict()


def test_pdf_verifier_fails_if_form_widgets_are_not_drawn(monkeypatch):
    source = _complex_two_page_form_pdf()
    adapter = PdfAdapter()
    target = next(item for item in adapter.targets(source) if item.id == "pdf:target")
    patch = Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                  TypedValue("string", "Alexandra"))
    plan = build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "synthetic-no-form-render"})
    candidate = adapter.apply(source, plan)
    render = pdf_verification._render

    def without_forms(data, scale=1.0, forms_enabled=True):
        return render(data, scale=scale, forms_enabled=False)

    monkeypatch.setattr(pdf_verification, "_render", without_forms)
    report = PdfIndependentVerifier().verify(source, candidate, plan)
    visual = next(item for item in report.checks if item.check == "visual")
    assert report.outcome == "BLOCKED", report.as_dict()
    assert visual.status in {"FAIL", "NOT_VERIFIED"}, report.as_dict()
