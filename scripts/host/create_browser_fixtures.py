#!/usr/bin/env python3
"""Create disposable synthetic PDF and DOCX browser fixtures under .runtime."""
from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import zipfile
from pathlib import Path

from docx import Document
from lxml import etree
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, ByteStringObject, DictionaryObject, NameObject,
                           NumberObject, TextStringObject)
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


def create_pdf() -> bytes:
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(612, 792))
    pdf.setTitle("Synthetic Channel360 PDF form")
    pdf.drawString(42, 748, "Synthetic Insurance Claim Form")
    pdf.drawString(42, 720, "Demonstration data only")
    form = pdf.acroForm
    form.textfield(name="claimant_name", tooltip="Claimant name", x=42, y=660, width=250, height=25,
                   value="Ada Example", maxlen=40, fontName="Helvetica", fontSize=12)
    form.checkbox(name="consent", tooltip="Consent", x=42, y=615, checked=False)
    form.radio(name="coverage", value="basic", x=42, y=570, selected=True)
    form.radio(name="coverage", value="extended", x=100, y=570)
    form.choice(name="region", x=42, y=510, value="CA", options=["CA", "NY", "TX"],
                fontName="Helvetica", fontSize=12)
    pdf.showPage()
    pdf.drawString(42, 748, "Synthetic continuation page")
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def create_text_only_pdf() -> bytes:
    """A digital PDF with an extractable text layer and no AcroForm fields (read-only, text-anchorable)."""
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(612, 792))
    pdf.setTitle("Synthetic Channel360 invoice without form fields")
    pdf.drawString(42, 748, "Synthetic Repair Invoice")
    pdf.drawString(42, 720, "Invoice number: INV-SYN-50417")
    pdf.drawString(42, 700, "Insured: Meera Iyer")
    pdf.drawString(42, 680, "Amount due: INR 18,250.00")
    pdf.drawString(42, 650, "Demonstration data only")
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def _rewrite_zip(source: bytes, edit, extra: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as zin, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zout.writestr(info, edit(info.filename, zin.read(info.filename)))
        for name, data in (extra or {}).items():
            zout.writestr(name, data)
    return output.getvalue()


def _claim_workbook() -> bytes:
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Claim"
    for row in (("Field", "Value"), ("Insured", "RICH_PLACEHOLDER"), ("Status", "Open"), ("Amount", 18250)):
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def create_rich_text_xlsx() -> bytes:
    """Editable workbook whose B2 is an inline rich-text cell (read-only target with visible text)."""
    def edit(name: str, data: bytes) -> bytes:
        if name != "xl/worksheets/sheet1.xml":
            return data
        start = data.find(b'<c r="B2"')
        end = data.find(b"</c>", start) + 4
        return data[:start] + (b'<c r="B2" t="inlineStr"><is><r><rPr><b/></rPr><t>Meera</t></r>'
                               b'<r><t xml:space="preserve"> Iyer</t></r></is></c>') + data[end:]
    return _rewrite_zip(_claim_workbook(), edit)


def create_macro_xlsm() -> bytes:
    """Workbook with a (synthetic, non-executable) vbaProject part: read-only, values still viewable."""
    return _rewrite_zip(_claim_workbook(), lambda name, data: data.replace(b"RICH_PLACEHOLDER", b"Meera Iyer"),
                        {"xl/vbaProject.bin": b"synthetic placeholder, not a macro project"})


def create_sections_docx() -> bytes:
    """A block recognised as two summary sections, so the summary cites the same block twice."""
    document = Document()
    document.add_heading("Claim information", level=1)
    document.add_paragraph("Claim number: CLM-SYN-0001")
    document.add_paragraph("Policy number: POL-SYN-9")
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def create_readonly_values_xlsx() -> bytes:
    """One workbook with every kind of read-only XLSX value next to a normal editable cell (B-1).

    Claim!A2/B2 editable; B3 formula; B4 inline rich text; B5 time of day; B6 list validation whose
    source is not a literal list or plain range (unsupported). Locked!A1 is locked on a protected
    sheet, Locked!A2 is explicitly unlocked on the same sheet."""
    from datetime import time as time_of_day
    from openpyxl import Workbook
    from openpyxl.styles import Protection
    from openpyxl.worksheet.datavalidation import DataValidation
    workbook = Workbook()
    claim = workbook.active
    claim.title = "Claim"
    claim["A1"], claim["B1"] = "Field", "Value"
    claim["A2"], claim["B2"] = "Insured", "Meera Iyer"
    claim["A3"], claim["B3"] = "Name length", "=LEN(B2)"
    claim["A4"], claim["B4"] = "Remark", "RICH_PLACEHOLDER"
    claim["A5"], claim["B5"] = "Survey time", time_of_day(15, 30)
    claim["B5"].number_format = "h:mm"
    claim["A6"], claim["B6"] = "Status", "Pending"
    validation = DataValidation(type="list", formula1="OFFSET($A$1,0,0,3,1)", allow_blank=False)
    claim.add_data_validation(validation)
    validation.add("B6")
    locked = workbook.create_sheet("Locked")
    locked["A1"] = "Locked reserve amount"
    locked["A2"] = "Unlocked adjuster note"
    locked["A2"].protection = Protection(locked=False)
    locked.protection.sheet = True
    output = io.BytesIO()
    workbook.save(output)

    def edit(name: str, data: bytes) -> bytes:
        if name != "xl/worksheets/sheet1.xml":
            return data
        start = data.find(b'<c r="B4"')
        end = data.find(b"</c>", start) + 4
        return data[:start] + (b'<c r="B4" t="inlineStr"><is><r><rPr><b/></rPr><t>Urgent:</t></r>'
                               b'<r><t xml:space="preserve"> tow required</t></r></is></c>') + data[end:]
    return _rewrite_zip(output.getvalue(), edit)


def create_docx() -> bytes:
    document = Document()
    document.add_heading("Synthetic Claim Summary", level=1)
    document.add_paragraph("Policyholder: Alexander Kumar")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Claim number"
    table.cell(0, 1).text = "CLM-DEMO-360"
    table.cell(1, 0).text = "Region"
    table.cell(1, 1).text = "California"
    document.sections[0].header.paragraphs[0].text = "Channel360 synthetic fixture"
    document.sections[0].footer.paragraphs[0].text = "No customer data"
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def create_encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt(user_password="synthetic-only", owner_password="synthetic-owner")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def create_xfa_pdf() -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(create_pdf())))
    writer._root_object["/AcroForm"].get_object()[NameObject("/XFA")] = TextStringObject("synthetic-xfa")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def create_restricted_pdf(restriction: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(create_pdf())))
    if restriction == "certified":
        permissions = DictionaryObject({NameObject("/DocMDP"): writer._root_object.indirect_reference})
        writer._root_object[NameObject("/Perms")] = writer._add_object(permissions)
    elif restriction == "signed":
        signature = DictionaryObject({
            NameObject("/FT"): NameObject("/Sig"),
            NameObject("/T"): TextStringObject("synthetic_signature"),
            NameObject("/V"): DictionaryObject({
                NameObject("/Type"): NameObject("/Sig"),
                NameObject("/ByteRange"): ArrayObject([NumberObject(0), NumberObject(1),
                                                         NumberObject(2), NumberObject(3)]),
                NameObject("/Contents"): ByteStringObject(b"synthetic-signature-marker"),
            }),
        })
        writer._root_object["/AcroForm"].get_object()["/Fields"].append(writer._add_object(signature))
    else:
        raise ValueError(f"unsupported synthetic restriction: {restriction}")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def create_scanned_pdf() -> bytes:
    with Image.new("RGB", (900, 1200), "white") as page_image:
        draw = ImageDraw.Draw(page_image)
        draw.text((80, 120), "Synthetic Scanned Insurance Claim", fill="black")
        draw.text((80, 190), "Demonstration data only", fill="black")
        image = ImageReader(page_image)
        output = io.BytesIO()
        pdf = canvas.Canvas(output, pagesize=(612, 792))
        pdf.drawImage(image, 0, 0, width=612, height=792)
        pdf.showPage()
        pdf.save()
        return output.getvalue()


def create_tracked_change_docx() -> bytes:
    source = create_docx()
    input_zip = zipfile.ZipFile(io.BytesIO(source))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for info in input_zip.infolist():
            content = input_zip.read(info.filename)
            if info.filename == "word/document.xml":
                root = etree.fromstring(content)
                paragraph = root.find(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p")
                deleted = etree.SubElement(paragraph, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}del")
                deleted.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}id", "7")
                deleted.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}author", "Synthetic fixture")
                run = etree.SubElement(deleted, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}r")
                etree.SubElement(run, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}delText").text = "Removed wording"
                content = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            archive.writestr(info, content)
    input_zip.close()
    return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".runtime/browser-fixtures")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "synthetic-form.pdf").write_bytes(create_pdf())
    if not shutil.which("qpdf"):
        raise SystemExit("qpdf is required to create the independent PDF producer fixture")
    intermediate = output / "synthetic-form-reportlab.pdf"
    intermediate.write_bytes(create_pdf())
    subprocess.run(["qpdf", "--object-streams=generate", str(intermediate),
                    str(output / "synthetic-form-qpdf.pdf")], check=True, capture_output=True)
    intermediate.unlink()
    (output / "synthetic-encrypted.pdf").write_bytes(create_encrypted_pdf())
    (output / "synthetic-xfa.pdf").write_bytes(create_xfa_pdf())
    (output / "synthetic-certified.pdf").write_bytes(create_restricted_pdf("certified"))
    (output / "synthetic-signed.pdf").write_bytes(create_restricted_pdf("signed"))
    (output / "synthetic-scanned.pdf").write_bytes(create_scanned_pdf())
    (output / "synthetic-invoice-no-fields.pdf").write_bytes(create_text_only_pdf())
    (output / "synthetic-rich-text.xlsx").write_bytes(create_rich_text_xlsx())
    (output / "synthetic-macro.xlsm").write_bytes(create_macro_xlsm())
    (output / "synthetic-sections.docx").write_bytes(create_sections_docx())
    (output / "synthetic-readonly-values.xlsx").write_bytes(create_readonly_values_xlsx())
    (output / "synthetic-claim.docx").write_bytes(create_docx())
    (output / "synthetic-tracked-changes.docx").write_bytes(create_tracked_change_docx())


if __name__ == "__main__":
    main()
