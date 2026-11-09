from __future__ import annotations

import io

from docx import Document
from reportlab.pdfgen import canvas

from packages.workers.parse_entry import inspect_bytes


def test_restricted_parser_extracts_pdf_fields_pages_and_scanned_state():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream)
    pdf.drawString(40, 760, "Synthetic insurance claim policyholder")
    pdf.acroForm.textfield(name="policyholder", x=40, y=700, width=180, height=24,
                           value="A. Example", maxlen=40)
    pdf.showPage(); pdf.save()
    result = inspect_bytes(stream.getvalue(), filename="synthetic-policy.pdf", include_targets=True)
    assert result["status"] == "inspected"
    assert result["adapter"] == "pdf"
    assert result["details"]["page_count"] == 1
    assert result["details"]["fields"][0]["editable"] is True
    assert result["details"]["pages"][0]["text"]
    assert result["targets"][0]["native_locator"]["field_name"] == "policyholder"
    assert result["structural_summary"]["statistics"]["acroform_field_count"] == 1
    assert "insurance" in result["structural_summary"]["description"]
    unbound = inspect_bytes(stream.getvalue(), filename="synthetic-policy.pdf")
    assert unbound["structural_summary"]["statistics"]["acroform_field_count"] == 1


def test_restricted_parser_extracts_docx_body_table_header_and_footer_targets():
    document = Document()
    document.add_paragraph("Synthetic body text")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "Synthetic table cell"
    section = document.sections[0]
    section.header.paragraphs[0].text = "Synthetic header"
    section.footer.paragraphs[0].text = "Synthetic footer"
    stream = io.BytesIO(); document.save(stream)
    result = inspect_bytes(stream.getvalue(), filename="synthetic-policy.docx", include_targets=True)
    assert result["status"] == "inspected"
    assert result["adapter"] == "docx"
    assert {block["kind"] for block in result["details"]["blocks"]} == {"paragraph", "table_cell", "header", "footer"}
    assert len([target for target in result["targets"] if target["editable"]]) == 4
    assert result["structural_summary"]["statistics"]["sections_by_kind"]["table_cell"] == 1


def test_structural_summary_marks_scanned_pages_without_claiming_ocr():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream); pdf.rect(20, 20, 500, 700, fill=1); pdf.showPage(); pdf.save()
    summary = inspect_bytes(stream.getvalue(), filename="scanned.pdf")["structural_summary"]
    assert summary["statistics"]["scanned_or_image_only_pages"] == 1
    assert any("without OCR" in note for note in summary["limitations"])


def test_ai_summary_evidence_uses_safe_locations_for_pdf_docx_and_scanned_pdf():
    pdf_stream = io.BytesIO()
    pdf = canvas.Canvas(pdf_stream)
    pdf.drawString(40, 760, "Synthetic claim reference SYN-PDF-1")
    pdf.showPage(); pdf.save()
    pdf_result = inspect_bytes(pdf_stream.getvalue(), filename="synthetic.pdf", include_summary_evidence=True)
    assert pdf_result["summary_evidence"][0]["id"] == "pdf:page:1"
    assert "SYN-PDF-1" in pdf_result["summary_evidence"][0]["text"]

    document = Document(); document.add_paragraph("Synthetic policy statement")
    document.add_heading("Synthetic coverage section", level=1)
    document.add_table(rows=1, cols=1).cell(0, 0).text = "Synthetic coverage row"
    document.sections[0].header.paragraphs[0].text = "Synthetic header text"
    document.sections[0].footer.paragraphs[0].text = "Synthetic footer text"
    for index in range(45):
        document.add_paragraph(f"Synthetic body paragraph {index}")
    stream = io.BytesIO(); document.save(stream)
    docx_result = inspect_bytes(stream.getvalue(), filename="synthetic.docx", include_summary_evidence=True)
    assert {item["location"].split()[1] for item in docx_result["summary_evidence"]} >= {"paragraph", "table_cell"}
    assert any("heading level 1" in item["location"] and "Synthetic coverage section" in item["text"]
               for item in docx_result["summary_evidence"])
    assert any(item["id"].startswith("word/header") and "Synthetic header text" in item["text"]
               for item in docx_result["summary_evidence"])
    assert any(item["id"].startswith("word/footer") and "Synthetic footer text" in item["text"]
               for item in docx_result["summary_evidence"])

    scanned_stream = io.BytesIO()
    scanned = canvas.Canvas(scanned_stream); scanned.rect(20, 20, 500, 700, fill=1); scanned.showPage(); scanned.save()
    scanned_result = inspect_bytes(scanned_stream.getvalue(), filename="scanned.pdf", include_summary_evidence=True)
    assert scanned_result["summary_evidence"] == [{"id": "pdf:scan-limitation:page:1",
        "location": "PDF page 1 inspection limitation",
        "text": "Page 1 has no readable extracted text. No OCR was performed; document content on this page cannot be reliably explained."}]
