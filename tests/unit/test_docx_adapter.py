from __future__ import annotations

import hashlib
import io
import zipfile
import xml.etree.ElementTree as ET

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from packages.core.adapters.docx.adapter import DocxAdapter
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import build_patch_plan
from packages.core.verification.docx import DocxIndependentVerifier


def _docx() -> bytes:
    document = Document()
    paragraph = document.add_paragraph()
    run = paragraph.add_run("Policyholder: Ada")
    run.bold = True
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "Claim number: C-100"
    section = document.sections[0]
    section.header.paragraphs[0].text = "Header: Renewal"
    section.footer.paragraphs[0].text = "Footer: Confidential"
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def test_docx_single_node_edits_cover_body_table_header_footer_and_package_provenance():
    source = _docx()
    adapter = DocxAdapter()
    targets = adapter.targets(source)
    replacements = {"Policyholder: Ada": "Policyholder: Amy", "Claim number: C-100": "Claim number: C-200",
                    "Header: Renewal": "Header: Revised", "Footer: Confidential": "Footer: Internal"}
    patches = [Patch(hashlib.sha256(source).hexdigest(), target.id, target.value, target.fingerprint,
                     TypedValue("string", replacements[target.value.value]))
               for target in targets if target.value.value in replacements]
    assert len(patches) == 4
    plan = build_patch_plan(adapter, source, patches,
                            provenance={"edit_set_id": "synthetic-docx-edit", "actor_id": "synthetic-processor"})
    candidate = adapter.apply(source, plan)
    with zipfile.ZipFile(io.BytesIO(source)) as before, zipfile.ZipFile(io.BytesIO(candidate)) as after:
        assert before.namelist() == after.namelist()[:len(before.namelist())]
        for part in before.namelist():
            if part not in {"word/document.xml", "word/header1.xml", "word/footer1.xml",
                            "docProps/custom.xml", "[Content_Types].xml", "_rels/.rels"}:
                assert before.read(part) == after.read(part)
    result = DocxIndependentVerifier().verify(source, candidate, plan)
    assert result.outcome == "BLOCKED"
    assert {check.check: check.status for check in result.checks} == {
        "integrity": "PASS", "policy": "PASS", "intent": "PASS", "preservation": "PASS",
        "provenance": "PASS", "visual": "NOT_VERIFIED",
    }
    visual = next(check for check in result.checks if check.check == "visual")
    assert visual.status == "NOT_VERIFIED", result.as_dict()


def test_docx_marks_multi_run_spans_and_hyperlinks_noneditable():
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("Policyholder: ")
    paragraph.add_run("Ada")
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink_run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "external link"
    hyperlink_run.append(text)
    hyperlink.append(hyperlink_run)
    paragraph._p.append(hyperlink)
    field_paragraph = document.add_paragraph()
    field = OxmlElement("w:fldSimple")
    field_run = OxmlElement("w:r")
    field_text = OxmlElement("w:t")
    field_text.text = "Page 1"
    field_run.append(field_text)
    field.append(field_run)
    field_paragraph._p.append(field)
    output = io.BytesIO()
    document.save(output)
    source = output.getvalue()
    targets = DocxAdapter().targets(source)
    assert any(target.reason == "multi_run_span" and not target.editable for target in targets)
    assert any(target.reason == "hyperlink" and not target.editable for target in targets)
    assert any(target.reason == "field_code" and not target.editable for target in targets)
    # An isolated single-run target remains valid; compound structures are read-only.
    assert any(target.editable and target.value.value == "Ada" for target in targets)


def _mutate_package(source: bytes, mutation) -> bytes:
    original = zipfile.ZipFile(io.BytesIO(source))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for info in original.infolist():
            archive.writestr(info, mutation(info.filename, original.read(info.filename)))
    original.close()
    return output.getvalue()


def test_docx_protected_and_macro_bearing_documents_are_read_only():
    source = _docx()
    settings_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def protect(name: str, content: bytes) -> bytes:
        if name != "word/settings.xml":
            return content
        root = ET.fromstring(content)
        protection = ET.SubElement(root, f"{{{settings_ns}}}documentProtection")
        protection.set(f"{{{settings_ns}}}edit", "readOnly")
        protection.set(f"{{{settings_ns}}}enforcement", "1")
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    protected = _mutate_package(source, protect)
    adapter = DocxAdapter()
    protected_profile = adapter.inspect(protected)
    assert protected_profile.level == "read_only"
    assert "document_protected" in protected_profile.reasons
    assert all(not target.editable for target in adapter.targets(protected))

    with zipfile.ZipFile(io.BytesIO(source)) as original:
        macro_bearing = io.BytesIO()
        with zipfile.ZipFile(macro_bearing, "w") as archive:
            for info in original.infolist():
                archive.writestr(info, original.read(info.filename))
            archive.writestr("word/vbaProject.bin", b"synthetic, non-executable macro marker")
    macro_profile = adapter.inspect(macro_bearing.getvalue())
    assert macro_profile.level == "read_only"
    assert "macros_present" in macro_profile.reasons
    assert all(not target.editable for target in adapter.targets(macro_bearing.getvalue()))


def test_docx_stale_fingerprint_and_tampered_untouched_part_are_blocked():
    source = _docx()
    adapter = DocxAdapter()
    target = next(item for item in adapter.targets(source) if item.value.value == "Policyholder: Ada")
    digest = hashlib.sha256(source).hexdigest()
    stale = Patch(digest, target.id, target.value, "0" * 64, TypedValue("string", "Policyholder: Amy"))
    import pytest
    from packages.core.adapters.docx.adapter import DocxDocumentError
    with pytest.raises(DocxDocumentError, match="fingerprint_mismatch"):
        adapter.preflight(source, target, stale)

    patch = Patch(digest, target.id, target.value, target.fingerprint, TypedValue("string", "Policyholder: Amy"))
    plan = build_patch_plan(adapter, source, [patch], provenance={"edit_set_id": "synthetic-tamper"})
    candidate = adapter.apply(source, plan)

    def tamper(name: str, content: bytes) -> bytes:
        return content + b"\n<!-- synthetic tampering -->" if name == "word/styles.xml" else content

    result = DocxIndependentVerifier().verify(source, _mutate_package(candidate, tamper), plan)
    assert result.outcome == "BLOCKED"
    assert next(check for check in result.checks if check.check == "preservation").status == "FAIL"


def test_docx_malformed_zip_is_rejected():
    import pytest
    from packages.core.adapters.docx.adapter import DocxDocumentError
    with pytest.raises(DocxDocumentError):
        DocxAdapter().inspect(b"not a word document")


def _render_test_pages(pages: list[list[tuple[float, str]]]) -> bytes:
    from reportlab.pdfgen import canvas

    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(612, 792))
    for page_lines in pages:
        for y, text in page_lines:
            pdf.drawString(40, y, text)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def _render_test_page(lines: list[tuple[float, str]]) -> bytes:
    return _render_test_pages([lines])


def test_docx_layout_check_rejects_unchanged_text_moved_after_target_edit():
    import packages.core.verification.docx as verifier

    compare = getattr(verifier, "_compare_rendered_layout", None)
    assert callable(compare), "DOCX visual verification must compare rendered glyph positions"
    source = _render_test_page([(740, "Policyholder: Ada"), (700, "Coverage: Standard")])
    same_layout = _render_test_page([(740, "Policyholder: Amy"), (700, "Coverage: Standard")])
    shifted = _render_test_page([(740, "Policyholder: Amy"), (680, "Coverage: Standard")])

    patch = [("Policyholder: Ada", "Policyholder: Amy")]
    assert compare(source, same_layout, patch)["errors"] == []
    assert "outside_content_position_changed" in compare(source, shifted, patch)["errors"]
    with_second_page = _render_test_pages([[(740, "Policyholder: Amy"), (700, "Coverage: Standard")],
                                           [(740, "Synthetic appendix")]])
    assert "page_count_changed" in compare(source, with_second_page, patch)["errors"]


def test_docx_layout_check_blocks_repeated_target_text_as_ambiguous():
    import packages.core.verification.docx as verifier

    compare = getattr(verifier, "_compare_rendered_layout", None)
    assert callable(compare), "DOCX visual verification must resolve target text unambiguously"
    source = _render_test_page([(740, "Policyholder: Ada"), (700, "Dependent: Ada")])
    candidate = _render_test_page([(740, "Policyholder: Amy"), (700, "Dependent: Amy")])

    result = compare(source, candidate, [("Ada", "Amy")])
    assert "ambiguous_target_render_match" in result["errors"]


def test_docx_layout_check_blocks_replacement_that_wraps_onto_another_line():
    import packages.core.verification.docx as verifier

    compare = getattr(verifier, "_compare_rendered_layout", None)
    assert callable(compare)
    source = _render_test_page([(740, "Policyholder: Ada"), (700, "Coverage: Standard")])
    wrapped = _render_test_page([(740, "Policyholder: A much longer policyholder"),
                                 (720, "name that wraps"), (700, "Coverage: Standard")])

    result = compare(source, wrapped,
                     [("Policyholder: Ada", "Policyholder: A much longer policyholder name that wraps")])
    assert "target_text_missing_or_wrapped" in result["errors"]


def test_docx_layout_check_detects_nontext_border_change_with_identical_glyph_geometry():
    import packages.core.verification.docx as verifier
    from reportlab.pdfgen import canvas

    def render(name: str, border: bool) -> bytes:
        output = io.BytesIO()
        pdf = canvas.Canvas(output, pagesize=(612, 792))
        pdf.drawString(40, 740, f"Policyholder: {name}")
        pdf.drawString(40, 700, "Coverage remains Standard")
        if border:
            pdf.setLineWidth(2)
            pdf.rect(30, 680, 250, 80, stroke=1, fill=0)
        pdf.showPage()
        pdf.save()
        return output.getvalue()

    result = verifier._compare_rendered_layout(
        render("Ada", False), render("Amy", True), [("Policyholder: Ada", "Policyholder: Amy")]
    )
    assert "outside_visual_pixels_changed" in result["errors"]
    assert result["pixels_changed_outside_targets"] > 0


def test_docx_layout_check_detects_changed_embedded_image_pixels():
    import packages.core.verification.docx as verifier
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    def render(name: str, color: str) -> bytes:
        image = Image.new("RGB", (24, 24), color)
        image_bytes = io.BytesIO()
        image.save(image_bytes, format="PNG")
        image_bytes.seek(0)
        output = io.BytesIO()
        pdf = canvas.Canvas(output, pagesize=(612, 792))
        pdf.drawString(40, 740, f"Policyholder: {name}")
        pdf.drawImage(ImageReader(image_bytes), 400, 600, width=24, height=24)
        pdf.showPage()
        pdf.save()
        return output.getvalue()

    result = verifier._compare_rendered_layout(
        render("Ada", "red"), render("Amy", "blue"), [("Policyholder: Ada", "Policyholder: Amy")]
    )
    assert "outside_visual_pixels_changed" in result["errors"]
    assert result["pixels_changed_outside_targets"] > 0


def test_docx_layout_check_detects_spacing_visual_changes():
    import packages.core.verification.docx as verifier
    from reportlab.pdfgen import canvas

    def render(name: str, line_x: int) -> bytes:
        output = io.BytesIO()
        pdf = canvas.Canvas(output, pagesize=(612, 792))
        pdf.drawString(40, 740, f"Policyholder: {name}")
        pdf.drawString(40, 700, "Coverage remains Standard")
        pdf.setLineWidth(1)
        pdf.line(line_x, 680, line_x + 120, 680)
        pdf.showPage()
        pdf.save()
        return output.getvalue()

    result = verifier._compare_rendered_layout(
        render("Ada", 200), render("Amy", 215), [("Policyholder: Ada", "Policyholder: Amy")]
    )
    assert "outside_visual_pixels_changed" in result["errors"]
