from __future__ import annotations

import struct
import zlib
from pathlib import Path
from copy import copy
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import numbers
from openpyxl.comments import Comment
from openpyxl.worksheet.datavalidation import DataValidation

def make_synthetic_png() -> bytes:
    """Return a deterministic RGB PNG showing a blue shield and green check."""
    width = height = 64
    background = (237, 244, 250)
    blue = (30, 75, 122)
    white = (255, 255, 255)
    green = (35, 153, 91)
    pixels = [[background for _ in range(width)] for _ in range(height)]

    def fill_polygon(points, color):
        for y in range(height):
            for x in range(width):
                inside = False
                previous = points[-1]
                for current in points:
                    x0, y0 = previous
                    x1, y1 = current
                    if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
                        inside = not inside
                    previous = current
                if inside:
                    pixels[y][x] = color

    def stroke(start, end, color, radius):
        x0, y0 = start
        x1, y1 = end
        dx, dy = x1 - x0, y1 - y0
        length_sq = dx * dx + dy * dy
        for y in range(height):
            for x in range(width):
                position = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / length_sq))
                nearest_x, nearest_y = x0 + position * dx, y0 + position * dy
                if (x - nearest_x) ** 2 + (y - nearest_y) ** 2 <= radius * radius:
                    pixels[y][x] = color

    fill_polygon([(9, 7), (55, 7), (53, 34), (46, 48), (32, 59), (18, 48), (11, 34)], blue)
    fill_polygon([(14, 12), (50, 12), (48, 33), (42, 44), (32, 52), (22, 44), (16, 33)], white)
    stroke((22, 31), (29, 38), green, 2.4)
    stroke((29, 38), (43, 24), green, 2.4)

    scanlines = b"".join(b"\x00" + b"".join(bytes(pixel) for pixel in row) for row in pixels)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(scanlines, level=9)) + chunk(b"IEND", b"")


PNG = make_synthetic_png()


def make_fixture(path: Path, *, dependent: bool = False, with_objects: bool = True, date1904: bool = False) -> Path:
    wb = Workbook()
    fixed_time = __import__("datetime").datetime(2026, 11, 6, 0, 0, 0)
    wb.properties.created = fixed_time
    wb.properties.modified = fixed_time
    if date1904:
        from openpyxl.utils.datetime import MAC_EPOCH
        wb.epoch = MAC_EPOCH
    ws = wb.active
    ws.title = "Claim"
    ws["A1"] = "Motor Claim"
    ws["A1"].comment = Comment("Synthetic fixture comment", "M0")
    ws["B2"] = "shared text"
    ws["C2"] = "shared text"
    ws["D2"] = 125.5
    ws["D2"].number_format = numbers.FORMAT_CURRENCY_USD_SIMPLE
    ws["E2"] = True
    ws["F2"] = 45678
    ws["F2"].number_format = "yyyy-mm-dd"
    ws["G2"] = "constant"
    ws["L2"] = 42
    ws["H2"] = "=D2*2"
    ws["I2"] = "=SUM(D2:D2)"
    ws["A3"] = "=INDIRECT(\"D2\")" if dependent else "safe"
    second = wb.create_sheet("Totals")
    second["A1"] = "='Claim'!D2*3"
    second["B1"] = "=SUM(Claim!D2:D2)"
    second["C1"] = "=ClaimAmount"
    if with_objects:
        chart = BarChart()
        chart.add_data(Reference(ws, min_col=4, min_row=2, max_row=2))
        ws.add_chart(chart, "K10")
        validation = DataValidation(type="list", formula1='"safe,unsafe"')
        validation.add("G2")
        ws.add_data_validation(validation)
        ws.conditional_formatting.add("D2", __import__("openpyxl").formatting.rule.CellIsRule(operator="greaterThan", formula=[100]))
        wb.defined_names.add(__import__("openpyxl").workbook.defined_name.DefinedName("ClaimAmount", attr_text="Claim!$D$2"))
    wb.calculation.fullCalcOnLoad = False
    wb.save(path)
    normalize_zip_timestamps(path)
    if with_objects:
        add_image_part(path)
    return path


def normalize_zip_timestamps(path: Path) -> None:
    with ZipFile(path) as zin:
        files = [(copy(info), zin.read(info.filename)) for info in zin.infolist()]
    tmp = path.with_suffix(".normalized.xlsx")
    with ZipFile(tmp, "w") as zout:
        for info, data in files:
            info.date_time = (2026, 11, 6, 0, 0, 0)
            zout.writestr(info, data, compress_type=info.compress_type)
    tmp.replace(path)


def add_image_part(path: Path) -> None:
    from lxml import etree

    xdr = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    ct = "http://schemas.openxmlformats.org/package/2006/content-types"
    with ZipFile(path) as zin:
        files = {i.filename: (i, zin.read(i.filename)) for i in zin.infolist()}
    drawing_name = next(n for n in files if n.startswith("xl/drawings/drawing") and n.endswith(".xml"))
    rels_name = drawing_name.replace("drawings/", "drawings/_rels/") + ".rels"
    drawing = etree.fromstring(files[drawing_name][1])
    anchor = etree.SubElement(drawing, f"{{{xdr}}}oneCellAnchor")
    marker = etree.SubElement(anchor, f"{{{xdr}}}from")
    for tag, value in (("col", "10"), ("colOff", "0"), ("row", "1"), ("rowOff", "0")):
        etree.SubElement(marker, f"{{{xdr}}}{tag}").text = value
    extent = etree.SubElement(anchor, f"{{{xdr}}}ext", cx="9525", cy="9525")
    picture = etree.SubElement(anchor, f"{{{xdr}}}pic")
    nv = etree.SubElement(picture, f"{{{xdr}}}nvPicPr")
    etree.SubElement(etree.SubElement(nv, f"{{{xdr}}}cNvPr"), f"{{{xdr}}}extLst")
    etree.SubElement(nv, f"{{{xdr}}}cNvPicPr")
    fill = etree.SubElement(picture, f"{{{xdr}}}blipFill")
    etree.SubElement(fill, f"{{{a}}}blip", attrib={"{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed": "rIdImage"})
    stretch = etree.SubElement(fill, f"{{{a}}}stretch")
    etree.SubElement(stretch, f"{{{a}}}fillRect")
    shape = etree.SubElement(picture, f"{{{xdr}}}spPr")
    geom = etree.SubElement(shape, f"{{{a}}}prstGeom", prst="rect")
    etree.SubElement(geom, f"{{{a}}}avLst")
    etree.SubElement(anchor, f"{{{xdr}}}clientData")
    files[drawing_name] = (files[drawing_name][0], etree.tostring(drawing, xml_declaration=True, encoding="UTF-8", standalone=True))
    if rels_name in files:
        relroot = etree.fromstring(files[rels_name][1])
    else:
        relroot = etree.Element(f"{{{rel}}}Relationships", nsmap={None: rel})
    etree.SubElement(relroot, f"{{{rel}}}Relationship", Id="rIdImage", Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image", Target="../media/image1.png")
    files[rels_name] = (files.get(rels_name, (None, b""))[0], etree.tostring(relroot, xml_declaration=True, encoding="UTF-8", standalone=True))
    files["xl/media/image1.png"] = (None, PNG)
    content = etree.fromstring(files["[Content_Types].xml"][1])
    if not content.xpath('./ct:Default[@Extension="png"]', namespaces={"ct": ct}):
        etree.SubElement(content, f"{{{ct}}}Default", Extension="png", ContentType="image/png")
    files["[Content_Types].xml"] = (files["[Content_Types].xml"][0], etree.tostring(content, xml_declaration=True, encoding="UTF-8", standalone=True))
    tmp = path.with_suffix(".image.xlsx")
    with ZipFile(tmp, "w") as zout:
        for name, (info, data) in files.items():
            zout.writestr(info or name, data, compress_type=info.compress_type if info else ZIP_DEFLATED)
    tmp.replace(path)


def add_shared_strings(path: Path) -> None:
    """Rewrite two inline string cells to share one standards-compliant SST entry."""
    from lxml import etree

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    with ZipFile(path) as zin:
        files = {i.filename: (i, zin.read(i.filename)) for i in zin.infolist()}
    sheet = etree.fromstring(files["xl/worksheets/sheet1.xml"][1])
    for ref, index in (("B2", "0"), ("C2", "0")):
        cell = sheet.xpath(f'.//m:c[@r="{ref}"]', namespaces={"m": ns})[0]
        cell.set("t", "s")
        for child in list(cell):
            cell.remove(child)
        etree.SubElement(cell, f"{{{ns}}}v").text = index
    files["xl/worksheets/sheet1.xml"] = (files["xl/worksheets/sheet1.xml"][0], etree.tostring(sheet, xml_declaration=True, encoding="UTF-8", standalone=True))
    sst = etree.Element(f"{{{ns}}}sst", nsmap={None: ns}, count="2", uniqueCount="1")
    si = etree.SubElement(sst, f"{{{ns}}}si")
    etree.SubElement(si, f"{{{ns}}}t").text = "shared text"
    files["xl/sharedStrings.xml"] = (None, etree.tostring(sst, xml_declaration=True, encoding="UTF-8", standalone=True))
    rels = etree.fromstring(files["xl/_rels/workbook.xml.rels"][1])
    etree.SubElement(rels, f"{{{rel_ns}}}Relationship", Id="rIdSST", Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings", Target="sharedStrings.xml")
    files["xl/_rels/workbook.xml.rels"] = (files["xl/_rels/workbook.xml.rels"][0], etree.tostring(rels, xml_declaration=True, encoding="UTF-8", standalone=True))
    types = etree.fromstring(files["[Content_Types].xml"][1])
    etree.SubElement(types, f"{{{ct_ns}}}Override", PartName="/xl/sharedStrings.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml")
    files["[Content_Types].xml"] = (files["[Content_Types].xml"][0], etree.tostring(types, xml_declaration=True, encoding="UTF-8", standalone=True))
    tmp = path.with_suffix(".tmp.xlsx")
    with ZipFile(tmp, "w") as zout:
        for name, (info, data) in files.items():
            zout.writestr(info or name, data, compress_type=info.compress_type if info else ZIP_DEFLATED)
    tmp.replace(path)


def add_unsupported_cells(path: Path) -> None:
    from lxml import etree

    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with ZipFile(path) as zin:
        files = {i.filename: (i, zin.read(i.filename)) for i in zin.infolist()}
    sheet = etree.fromstring(files["xl/worksheets/sheet1.xml"][1])
    data = sheet.find(f"{{{ns}}}sheetData")
    row = next(r for r in data if r.get("r") == "2")
    rich = etree.SubElement(row, f"{{{ns}}}c", r="J2", t="inlineStr")
    inline = etree.SubElement(rich, f"{{{ns}}}is")
    run = etree.SubElement(inline, f"{{{ns}}}r")
    etree.SubElement(etree.SubElement(run, f"{{{ns}}}rPr"), f"{{{ns}}}b")
    etree.SubElement(run, f"{{{ns}}}t").text = "two"
    run2 = etree.SubElement(inline, f"{{{ns}}}r")
    etree.SubElement(run2, f"{{{ns}}}t").text = " runs"
    array = etree.SubElement(row, f"{{{ns}}}c", r="K2")
    etree.SubElement(array, f"{{{ns}}}f", t="array", ref="K2:K3").text = "SUM(D2:D2)"
    etree.SubElement(array, f"{{{ns}}}v").text = "125.5"
    files["xl/worksheets/sheet1.xml"] = (files["xl/worksheets/sheet1.xml"][0], etree.tostring(sheet, xml_declaration=True, encoding="UTF-8", standalone=True))
    tmp = path.with_suffix(".unsupported.xlsx")
    with ZipFile(tmp, "w") as zout:
        for info, data in files.values():
            zout.writestr(info, data, compress_type=info.compress_type)
    tmp.replace(path)


@pytest.fixture
def workbook_path(tmp_path: Path) -> Path:
    path = make_fixture(tmp_path / "claim.xlsx")
    add_shared_strings(path)
    add_unsupported_cells(path)
    return path


@pytest.fixture
def dependent_path(tmp_path: Path) -> Path:
    path = make_fixture(tmp_path / "dependent.xlsx", dependent=True)
    add_shared_strings(path)
    add_unsupported_cells(path)
    return path
