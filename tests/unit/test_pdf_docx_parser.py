from __future__ import annotations

import io

from docx import Document
from reportlab.pdfgen import canvas

from packages.workers.parse_entry import inspect_bytes


def test_restricted_parser_extracts_pdf_fields_pages_and_scanned_state():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream)
    pdf.drawString(40, 760, "Synthetic policy document")
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
