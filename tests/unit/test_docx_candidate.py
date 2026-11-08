from __future__ import annotations

import json
import hashlib
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from lxml import etree

from packages.core.adapters.docx.adapter import DocxAdapter
from packages.core.models.contracts import Patch, TypedValue
from packages.core.planning.planner import build_patch_plan
from packages.core.verification.docx_candidate import validate_candidate


def test_supplied_pair_is_structurally_valid_and_keeps_visual_status_unverified():
    fixture = Path(__file__).parents[1] / "fixtures" / "docx" / "manual_validation"
    result = validate_candidate(
        (fixture / "original.docx").read_bytes(),
        (fixture / "candidate.docx").read_bytes(),
        json.loads((fixture / "expected-changes.json").read_text()),
    )

    assert result["structural_status"] == "PASS"
    assert result["visual_status"] == "NOT_VERIFIED"
    assert result["overall_status"] == "NOT_VERIFIED"
    assert result["hashes"]["source"] == "ad29bde1f5f30840888f28d44f37bdaf7eae06807fa6d176700d4723ef65aa9f"
    assert result["hashes"]["candidate"] == "50ce49d2fd069e0f12524041ce9b63ff130c6864babe7aa35643427c803e8afb"
    assert result["checks"]["target_values_fingerprints"]["status"] == "PASS"
    assert result["checks"]["unexpected_package_changes"]["status"] == "PASS"
    assert result["release_authorized"] is False


def _package(document: Document) -> bytes:
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _candidate_pair(source: bytes, replacements: list[tuple[str, str, str | None]]):
    adapter = DocxAdapter()
    targets = adapter.targets(source)
    patches = []
    changes = []
    for old, new, part in replacements:
        matches = [target for target in targets if target.value.value == old and
                   (part is None or target.part == part)]
        assert matches, (old, part)
        for target in matches[:1]:
            patches.append(Patch(hashlib.sha256(source).hexdigest(), target.id, target.value,
                                 target.fingerprint, TypedValue("string", new)))
            changes.append({"target_id": target.id, "old_value": old, "new_value": new,
                            "part": target.part, "target_fingerprint": target.fingerprint})
    plan = build_patch_plan(adapter, source, patches,
                            provenance={"edit_set_id": "offline-synthetic-test", "actor_id": "test"})
    candidate = adapter.apply(source, plan)
    expected = {"source_sha256": hashlib.sha256(source).hexdigest(),
                "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
                "edit_set_id": "offline-synthetic-test", "patch_plan_sha256": plan.plan_sha256,
                "changes": changes}
    return candidate, expected


@pytest.mark.parametrize("surface", ["body", "table", "header", "footer", "long", "multipage", "formatted", "tables_image"])
def test_synthetic_surfaces_validate_structurally_and_visual_is_not_verified(surface):
    document = Document()
    if surface == "body":
        document.add_paragraph("Body single-run: Old")
        replacements = [("Body single-run: Old", "Body single-run: New", "word/document.xml")]
    elif surface == "table":
        document.add_table(rows=1, cols=1).cell(0, 0).text = "Table value: Old"
        replacements = [("Table value: Old", "Table value: New", "word/document.xml")]
    elif surface == "header":
        document.sections[0].header.paragraphs[0].text = "Header value: Old"
        replacements = [("Header value: Old", "Header value: New", "word/header1.xml")]
    elif surface == "footer":
        document.sections[0].footer.paragraphs[0].text = "Footer value: Old"
        replacements = [("Footer value: Old", "Footer value: New", "word/footer1.xml")]
    elif surface == "long":
        document.add_paragraph("Name: A")
        replacements = [("Name: A", "Name: A substantially longer synthetic replacement name", "word/document.xml")]
    elif surface == "multipage":
        document.add_paragraph("Page one value: Old")
        document.add_page_break()
        document.add_paragraph("Page two unaffected content")
        replacements = [("Page one value: Old", "Page one value: New", "word/document.xml")]
    elif surface == "formatted":
        paragraph = document.add_paragraph()
        run = paragraph.add_run("Formatted: Old")
        run.bold = True
        run.font.name = "Arial"
        run.font.size = Pt(18)
        replacements = [("Formatted: Old", "Formatted: New", "word/document.xml")]
    else:
        from PIL import Image
        image_data = io.BytesIO()
        Image.new("RGB", (12, 12), "navy").save(image_data, format="PNG")
        image_data.seek(0)
        document.add_paragraph("Image and tables remain unchanged")
        document.add_picture(image_data)
        for index in range(2):
            document.add_table(rows=1, cols=2).cell(0, 0).text = f"Table {index}: Old"
        replacements = [("Table 0: Old", "Table 0: New", "word/document.xml")]
    source = _package(document)
    candidate, expected = _candidate_pair(source, replacements)
    result = validate_candidate(source, candidate, expected)
    assert result["structural_status"] == "PASS", result
    assert result["visual_status"] == "NOT_VERIFIED"
    assert result["release_authorized"] is False


def test_repeated_text_targets_are_validated_by_target_identifier():
    document = Document()
    document.add_paragraph("Status: Pending")
    document.add_paragraph("Status: Pending")
    source = _package(document)
    targets = [target for target in DocxAdapter().targets(source) if target.value.value == "Status: Pending"]
    assert len(targets) == 2
    patches = [Patch(hashlib.sha256(source).hexdigest(), targets[1].id, targets[1].value,
                     targets[1].fingerprint, TypedValue("string", "Status: Approved"))]
    plan = build_patch_plan(DocxAdapter(), source, patches,
                            provenance={"edit_set_id": "repeat-target", "actor_id": "test"})
    candidate = DocxAdapter().apply(source, plan)
    expected = {"source_sha256": hashlib.sha256(source).hexdigest(),
                "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
                "edit_set_id": "repeat-target", "patch_plan_sha256": plan.plan_sha256,
                "changes": [{"target_id": targets[1].id, "old_value": "Status: Pending",
                             "new_value": "Status: Approved", "part": targets[1].part,
                             "target_fingerprint": targets[1].fingerprint}]}
    result = validate_candidate(source, candidate, expected)
    assert result["structural_status"] == "PASS", result


def test_validator_rejects_stale_fingerprint_and_unexpected_xml_changes():
    document = Document()
    document.add_paragraph("Field: Original")
    source = _package(document)
    candidate, expected = _candidate_pair(source, [("Field: Original", "Field: Corrected", "word/document.xml")])
    stale = json.loads(json.dumps(expected))
    stale["changes"][0]["target_fingerprint"] = "0" * 64
    stale_result = validate_candidate(source, candidate, stale)
    assert stale_result["structural_status"] == "FAIL"
    assert "target_fingerprint_mismatch" in stale_result["checks"]["target_values_fingerprints"]["errors"][0]

    entries = []
    with zipfile.ZipFile(io.BytesIO(candidate)) as archive:
        for info in archive.infolist():
            data = archive.read(info.filename)
            if info.filename == "word/document.xml":
                data = data.replace(b"<w:body", b"<!-- unexpected -->\n<w:body", 1)
            entries.append((info, data))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for info, data in entries:
            archive.writestr(info, data)
    tampered = output.getvalue()
    expected["candidate_sha256"] = hashlib.sha256(tampered).hexdigest()
    tampered_result = validate_candidate(source, tampered, expected)
    assert tampered_result["structural_status"] == "FAIL"
    assert any("unexpected_xml_change:word/document.xml" in error
               for error in tampered_result["checks"]["unexpected_package_changes"]["errors"])


def test_validator_rejects_candidate_hash_mismatch():
    fixture = Path(__file__).parents[1] / "fixtures" / "docx" / "manual_validation"
    expected = json.loads((fixture / "expected-changes.json").read_text())
    expected["candidate_sha256"] = "f" * 64
    result = validate_candidate((fixture / "original.docx").read_bytes(),
                                (fixture / "candidate.docx").read_bytes(), expected)
    assert result["structural_status"] == "FAIL"
    assert "candidate_sha256_mismatch" in result["checks"]["hashes"]["errors"]


def test_cli_writes_json_and_markdown_without_authorizing_release(tmp_path):
    fixture = Path(__file__).parents[1] / "fixtures" / "docx" / "manual_validation"
    json_out, markdown_out = tmp_path / "result.json", tmp_path / "result.md"
    command = [sys.executable, "scripts/validate_docx_candidate.py",
               "--original", str(fixture / "original.docx"),
               "--candidate", str(fixture / "candidate.docx"),
               "--expected", str(fixture / "expected-changes.json"),
               "--json-out", str(json_out), "--markdown-out", str(markdown_out)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 3, result.stderr  # structural diagnostic passed; visual remains NOT_VERIFIED
    report = json.loads(json_out.read_text())
    assert report["overall_status"] == "NOT_VERIFIED"
    assert report["release_authorized"] is False
    assert report["external_evidence"]["status"] == "USER_SUPPLIED_NOT_REPRODUCED"
    assert "Visual: **NOT_VERIFIED**" in markdown_out.read_text()


@pytest.mark.parametrize("unsupported", ["multi_run", "protected", "tracked_changes"])
def test_unsupported_docx_cases_remain_read_only(unsupported):
    document = Document()
    if unsupported == "multi_run":
        paragraph = document.add_paragraph()
        paragraph.add_run("Synthetic name: ")
        paragraph.add_run("Ada")
    else:
        document.add_paragraph("Synthetic name: Ada")
    source = _package(document)
    if unsupported == "protected":
        def add_protection(name, data):
            if name == "word/settings.xml":
                xml = etree.fromstring(data)
                node = OxmlElement("w:documentProtection")
                node.set(qn("w:edit"), "readOnly")
                node.set(qn("w:enforcement"), "1")
                xml.append(node)
                return etree.tostring(xml, xml_declaration=True, encoding="UTF-8", standalone=True)
            return data
        source = _mutate_docx_candidate(source, add_protection)
    elif unsupported == "tracked_changes":
        def add_tracked(name, data):
            if name == "word/document.xml":
                root = etree.fromstring(data)
                text = root.xpath(".//w:t", namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"})[0]
                insertion = etree.Element("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}ins")
                run = text.getparent()
                paragraph = run.xpath("ancestor::w:p[1]", namespaces={
                    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"})[0]
                paragraph.replace(run, insertion)
                insertion.append(run)
                return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            return data
        source = _mutate_docx_candidate(source, add_tracked)
    profile = DocxAdapter().inspect(source)
    targets = DocxAdapter().targets(source)
    if unsupported == "multi_run":
        assert any(target.reason == "multi_run_span" and not target.editable for target in targets)
    else:
        assert profile.level == "read_only", (unsupported, profile)
        assert all(not target.editable for target in targets), unsupported


def test_validator_compares_optional_rendered_pdf_evidence_but_never_authorizes_release():
    from reportlab.pdfgen import canvas

    document = Document()
    document.add_paragraph("Policyholder: Ada")
    source = _package(document)
    candidate, expected = _candidate_pair(source, [("Policyholder: Ada", "Policyholder: Amy", "word/document.xml")])

    def render(name: str, extra_border: bool = False) -> bytes:
        output = io.BytesIO()
        pdf = canvas.Canvas(output, pagesize=(612, 792))
        pdf.drawString(40, 740, f"Policyholder: {name}")
        if extra_border:
            pdf.rect(30, 700, 200, 60, stroke=1, fill=0)
        pdf.showPage()
        pdf.save()
        return output.getvalue()

    visual_pass = validate_candidate(source, candidate, expected,
                                     rendered_original_pdf=render("Ada"),
                                     rendered_candidate_pdf=render("Amy"))
    assert visual_pass["visual_status"] == "PASS"
    assert visual_pass["overall_status"] == "DIAGNOSTIC_PASS_NOT_A_RELEASE"
    assert visual_pass["release_authorized"] is False

    visual_fail = validate_candidate(source, candidate, expected,
                                     rendered_original_pdf=render("Ada"),
                                     rendered_candidate_pdf=render("Amy", extra_border=True))
    assert visual_fail["visual_status"] == "FAIL"
    assert "outside_visual_pixels_changed" in visual_fail["checks"]["visual"]["errors"]
    assert visual_fail["release_authorized"] is False


def _mutate_docx_candidate(source: bytes, mutation) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(output, "w") as candidate:
        for info in original.infolist():
            candidate.writestr(info, mutation(info.filename, original.read(info.filename)))
    return output.getvalue()
