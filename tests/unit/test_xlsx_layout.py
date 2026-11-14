"""Layout extraction for the editable grid (synthetic workbook, no stakeholder data)."""
import io

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image

from packages.core.xlsx_layout import extract


def _book() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Quote"
    ws["A1"], ws["A2"], ws["C3"] = "Title", "=SUM(1,2)", "Value"
    ws.merge_cells("A1:C1")
    ws.merge_cells("B4:C5")
    ws.column_dimensions["A"].width = 30
    ws.row_dimensions[2].height = 42
    ws["A1"].font = Font(bold=True, italic=True, size=14, color="FF0000")
    ws["A1"].fill = PatternFill("solid", fgColor="DDEEFF")
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    buf = io.BytesIO()
    Image.new("RGB", (30, 20), (255, 0, 0)).save(buf, "PNG")
    buf.seek(0)
    ws.add_image(XLImage(buf), "E2")
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_merges_widths_heights_styles_and_picture():
    layout = extract(_book())
    sheet = layout["sheets"][0]
    assert sheet["name"] == "Quote"
    assert sorted(sheet["merges"]) == [[1, 1, 1, 3], [4, 2, 5, 3]]
    assert sheet["cols"]["1"] == 30 and sheet["rows"]["2"] == 42
    style = layout["styles"][sheet["cells"]["A1"]]
    assert style["b"] == 1 and style["i"] == 1 and style["sz"] == 14 and style["c"] == "#FF0000"
    assert style["bg"] == "#DDEEFF" and style["ha"] == "center" and style["va"] == "center" and style["wrap"] == 1
    assert len(sheet["images"]) == 1 and sheet["images"][0]["src"].startswith("data:image/png;base64,")
    assert sheet["images"][0]["from"]["col"] == 4 and sheet["images"][0]["from"]["row"] == 1


def test_garbage_is_rejected_not_crashed():
    assert extract(b"not a zip") is None
    bad = io.BytesIO()
    import zipfile
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("xl/workbook.xml", "<not-xml")
    assert extract(bad.getvalue()) is None


def test_external_pictures_and_oversize_media_are_never_embedded():
    """Only the package's own, size-capped media is embedded; nothing is fetched."""
    import zipfile
    src = _book()
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename.startswith("xl/media/"):
                data = b"\x89PNG" + b"0" * (700 * 1024)       # above the 600 KB cap
            zout.writestr(item, data)
    assert extract(out.getvalue())["sheets"][0]["images"] == []
