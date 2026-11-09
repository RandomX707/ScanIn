"""Phase 3A item 2: read-only targets report editable=false consistently and rich text shows its text."""
from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from packages.workers.parse_entry import _apply_document_level_editability, inspect_bytes


def _workbook() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Claim"
    sheet["A1"], sheet["B1"] = "Field", "Value"
    sheet["A2"], sheet["B2"] = "Insured", "PLACEHOLDER_SHARED"
    sheet["A3"], sheet["B3"] = "Status", "Open"
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _rewrite(source: bytes, edit, extra: dict[str, bytes] | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zout.writestr(info, edit(info.filename, zin.read(info.filename)))
        for name, data in (extra or {}).items():
            zout.writestr(name, data)
    return out.getvalue()


def _rich_workbook() -> bytes:
    """B2 is a shared-string rich-text cell, B3 an inline rich-text cell (two runs each)."""
    shared = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1" uniqueCount="1">'
              b'<si><r><rPr><b/></rPr><t>Rahul</t></r><r><t xml:space="preserve"> Sharma</t></r></si></sst>')

    def replace_cell(data: bytes, ref: bytes, xml: bytes) -> bytes:
        index = data.find(b'<c r="' + ref + b'"')
        end = data.find(b"</c>", index) + 4
        return data[:index] + xml + data[end:]

    def edit(name: str, data: bytes) -> bytes:
        if name == "xl/worksheets/sheet1.xml":
            data = replace_cell(data, b"B2", b'<c r="B2" t="s"><v>0</v></c>')
            return replace_cell(data, b"B3", b'<c r="B3" t="inlineStr"><is><r><rPr><i/></rPr><t>Under</t></r>'
                                             b'<r><t xml:space="preserve"> review</t></r></is></c>')
        if name == "[Content_Types].xml":
            return data.replace(b"</Types>", b'<Override PartName="/xl/sharedStrings.xml" ContentType="application/'
                                b'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>')
        if name == "xl/_rels/workbook.xml.rels":
            return data.replace(b"</Relationships>", b'<Relationship Id="rIdShared" Type="http://schemas.openxmlformats.org/'
                                b'officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>')
        return data
    return _rewrite(_workbook(), edit, {"xl/sharedStrings.xml": shared})


def test_rich_text_cells_show_their_text_but_stay_read_only():
    source = _rich_workbook()
    result = inspect_bytes(source, filename="claim.xlsx", include_targets=True)
    assert result["capability"]["level"] == "editable"
    targets = {t["id"]: t for t in result["targets"]}
    for target_id, text in (("xlsx:Claim!B2", "Rahul Sharma"), ("xlsx:Claim!B3", "Under review")):
        assert targets[target_id]["value"]["type"] == "string" and targets[target_id]["value"]["value"] == text
        assert targets[target_id]["editable"] is False and targets[target_id]["reason"] == "rich_text"
    assert targets["xlsx:Claim!A2"]["editable"] is True
    lookup = inspect_bytes(source, filename="claim.xlsx", include_targets=True, target_ids=["xlsx:Claim!B2"])
    assert lookup["targets"] == [targets["xlsx:Claim!B2"]]


def test_macro_workbook_reports_every_target_read_only():
    source = _rewrite(_workbook(), lambda name, data: data, {"xl/vbaProject.bin": b"synthetic-not-a-real-macro"})
    result = inspect_bytes(source, filename="claim.xlsm", include_targets=True)
    assert result["capability"] == {"level": "Read-only", "reasons": ["macros_present"]}
    assert result["targets"] and all(t["editable"] is False for t in result["targets"])
    assert {t["reason"] for t in result["targets"]} == {"macros_present"}
    assert next(t for t in result["targets"] if t["id"] == "xlsx:Claim!B3")["value"]["value"] == "Open"


def test_editable_workbook_targets_are_unchanged():
    result = inspect_bytes(_workbook(), filename="claim.xlsx", include_targets=True)
    assert result["capability"]["level"] == "editable" and all(t["editable"] for t in result["targets"])


@pytest.mark.parametrize("level,expected_editable", [("read_only", False), ("limited", False), ("editable", True)])
def test_document_level_rule_covers_targets_pdf_fields_and_docx_targets(level, expected_editable):
    result = {"targets": [{"id": "a", "editable": True, "reason": None}, {"id": "b", "editable": False, "reason": "rich_text"}],
              "details": {"fields": [{"id": "pdf:x", "editable": True, "reason": None}],
                          "targets": [{"id": "docx:y", "editable": True, "reason": None}]}}
    _apply_document_level_editability(result, SimpleNamespace(level=level, reasons=("signed_pdf",)))
    assert result["targets"][0]["editable"] is expected_editable
    assert result["targets"][1] == {"id": "b", "editable": False, "reason": "rich_text"}
    assert result["details"]["fields"][0]["editable"] is expected_editable
    assert result["details"]["targets"][0]["editable"] is expected_editable
    if not expected_editable:
        assert result["targets"][0]["reason"] == result["details"]["fields"][0]["reason"] == "signed_pdf"
