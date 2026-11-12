"""Empty template fields are editable targets; merged-over cells are not (synthetic workbook)."""
import io

from openpyxl import Workbook
from openpyxl.styles import PatternFill

from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.models.contracts import TypedValue


def _template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "RFQ"
    fill = PatternFill("solid", fgColor="FFFFCC")
    ws["A1"], ws["A2"], ws["A3"] = "Insured name", "Sum insured", "Total"
    for ref in ("B1", "B2", "B3", "C3", "D3"):
        ws[ref].fill = fill                              # styled but empty: <c r=.. s=../>
    ws.merge_cells("B3:D3")                              # C3, D3 are hidden under the merge
    ws["B4"] = "=B2*2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_blank_fields_are_editable_and_covered_cells_are_not():
    targets = {t.cell: t for t in XlsxAdapter().targets(_template())}
    assert targets["B1"].value.type == "null" and targets["B1"].editable
    assert targets["B2"].value.type == "null" and targets["B2"].editable
    assert targets["B3"].editable                         # the merge anchor is the one fillable cell
    assert "C3" not in targets and "D3" not in targets    # covered cells never appear
    assert targets["A1"].value.value == "Insured name"


def test_empty_field_accepts_plain_values_but_nothing_else_changes_rules():
    a = XlsxAdapter()
    empty = TypedValue("null", None)
    assert all(a.type_compatible(empty, TypedValue(t, v)) for t, v in (("string", "x"), ("integer", 5), ("decimal", 1.5), ("boolean", True)))
    assert not a.type_compatible(empty, TypedValue("date", "2026-01-01"))
    assert not a.type_compatible(TypedValue("string", "a"), TypedValue("integer", 1))      # unchanged strictness
    assert a.type_compatible(TypedValue("integer", 1), TypedValue("decimal", 1.5))


def test_save_coercion_infers_number_or_text_for_an_empty_field():
    from packages.api.save_flow import _coerce
    assert _coerce("null", "12345") == {"type": "integer", "value": 12345}
    assert _coerce("null", "7.5") == {"type": "decimal", "value": "7.5"}
    assert _coerce("null", "007") == {"type": "string", "value": "007"}                    # IDs keep leading zeros
    assert _coerce("null", "Acme Ltd") == {"type": "string", "value": "Acme Ltd"}
    assert _coerce("null", "1" * 20) == {"type": "string", "value": "1" * 20}              # beyond Excel precision stays text
