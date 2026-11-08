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
