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


def create_docx() -> bytes:
    document = Document()
    document.add_heading("Synthetic Claim Summary", level=1)
    document.add_paragraph("Policyholder: Ada Example")
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
    (output / "synthetic-claim.docx").write_bytes(create_docx())
    (output / "synthetic-tracked-changes.docx").write_bytes(create_tracked_change_docx())


if __name__ == "__main__":
    main()
