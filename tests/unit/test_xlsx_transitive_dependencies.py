"""Phase 3B (B6): transitive XLSX formula dependencies across sheets, conservative when unresolvable."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook

from packages.core.adapters.xlsx.adapter import XlsxAdapter, _translate_shared_formula


def _book(cells: dict[str, dict[str, object]]) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet_name, values in cells.items():
        sheet = workbook.create_sheet(sheet_name)
        for ref, value in values.items():
            sheet[ref] = value
    stream = io.BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _deps(source: bytes, target_id: str):
    adapter = XlsxAdapter()
    target = next(item for item in adapter.targets(source) if item.id == target_id)
    return target.dependents, adapter.formula_dependents(source, target)


def test_multi_hop_cross_sheet_chain_is_reported_completely():
    source = _book({"Claim": {"D5": 100, "D6": 50, "D12": "=SUM(D5:D11)", "D13": 10, "D14": "=D12-D13", "E1": "=D6*2"},
                    "Summary": {"B1": "=Claim!D14*1.18", "B2": 7}})
    dependents, (planned, unknown) = _deps(source, "xlsx:Claim!D5")
    assert dependents == planned == ("Claim!D12", "Claim!D14", "Summary!B1") and unknown is False
    assert _deps(source, "xlsx:Summary!B2")[0] == ()          # independent cell: nothing affected
    assert _deps(source, "xlsx:Claim!D6") == (("Claim!D12", "Claim!D14", "Claim!E1", "Summary!B1"),
                                              (("Claim!D12", "Claim!D14", "Claim!E1", "Summary!B1"), False))


def test_quoted_sheet_names_with_spaces_and_escaped_quotes_and_absolute_ranges():
    source = _book({"Claim Data": {"A1": 5, "A2": 6},
                    "Bob's Totals": {"B1": "=SUM('Claim Data'!$A$1:$A$2)", "B2": "='Bob''s Totals'!B1+1"},
                    "Report": {"C1": "='Bob''s Totals'!B2*2"}})
    dependents, (_, unknown) = _deps(source, "xlsx:Claim Data!A2")
    assert dependents == ("Bob's Totals!B1", "Bob's Totals!B2", "Report!C1") and unknown is False


def test_cycles_terminate_without_duplicates():
    source = _book({"S": {"A1": 1, "B1": "=A1+C1", "C1": "=B1*2", "D1": "=C1"}})
    dependents, (_, unknown) = _deps(source, "xlsx:S!A1")
    assert dependents == ("S!B1", "S!C1", "S!D1") and unknown is False


@pytest.mark.parametrize("formula", ['=INDIRECT("A1")', "=OFFSET(A1,1,0)", "=[1]Other!A1", "=Missing!A1", "=A:A", "=VLOOKUP(A1,B1:C2,2)"])
def test_unresolvable_references_are_reported_as_unknown(formula):
    source = _book({"S": {"A1": 1, "B1": "=A1*2", "Z9": formula}})
    dependents, (planned, unknown) = _deps(source, "xlsx:S!A1")
    assert dependents is None and unknown is True       # never claims complete coverage


def _with_shared_formulas(source: bytes, sheet_xml_edit) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            zout.writestr(info, sheet_xml_edit(data) if info.filename == "xl/worksheets/sheet1.xml" else data)
    return out.getvalue()


def test_shared_formula_children_are_translated_not_silently_ignored():
    base = _book({"S": {"A1": 1, "A2": 2, "A3": 3, "B1": "=A1*2", "B2": "=A2*2", "B3": "=A3*2", "C3": "=B3+1"}})

    def make_shared(xml: bytes) -> bytes:
        xml = xml.replace(b"<f>A1*2</f>", b'<f t="shared" ref="B1:B3" si="0">A1*2</f>')
        return xml.replace(b"<f>A2*2</f>", b'<f t="shared" si="0"/>').replace(b"<f>A3*2</f>", b'<f t="shared" si="0"/>')
    source = _with_shared_formulas(base, make_shared)
    assert b'<f t="shared" si="0"/>' in zipfile.ZipFile(io.BytesIO(source)).read("xl/worksheets/sheet1.xml")
    assert _deps(source, "xlsx:S!A3")[0] == ("S!B3", "S!C3")
    assert _deps(source, "xlsx:S!A2")[0] == ("S!B2",)
    orphan = _with_shared_formulas(base, lambda xml: xml.replace(b"<f>A2*2</f>", b'<f t="shared" si="7"/>'))
    assert _deps(orphan, "xlsx:S!A1")[1][1] is True     # child without a master: dependencies unknown


@pytest.mark.parametrize("master,master_ref,child_ref,expected", [
    ("A1*2", "B1", "B3", "A3*2"),
    ("$A$1+A1", "B1", "C2", "$A$1+B2"),
    ("SUM('Claim Data'!A1:A2)", "B1", "B2", "SUM('Claim Data'!A2:A3)"),
    ("A$1+$A1", "B1", "D4", "C$1+$A4"),
    ("A1", "B5", "B1", None),                              # would move above row 1
])
def test_shared_formula_translation(master, master_ref, child_ref, expected):
    assert _translate_shared_formula(master, master_ref, child_ref) == expected


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "xlsx" / "xlsx-03-formulas-cross-sheet.xlsx"  # E-XLSX-07 fixture copy


def test_e_xlsx_07_fixture_reports_the_full_chain():
    dependents, (planned, unknown) = _deps(FIXTURE.read_bytes(), "xlsx:Claim!D5")
    assert dependents == planned == ("Claim!D12", "Claim!D14", "Summary!B1") and unknown is False
