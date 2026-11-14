"""Pure comparison engine: alignment, kinds, locations, honest limits."""
from packages.core import compare as c


def page(n, *lines):
    return {"page": n, "width": 600, "height": 800, "lines": [{"t": t, "bb": [10, 700 - 20 * i, 200, 712 - 20 * i]} for i, t in enumerate(lines)]}


def test_pdf_modified_added_removed_with_boxes():
    base = {"pages": [page(1, "Policy Number 123", "Insured: Alpha Ltd", "Premium 1000"), page(2, "Terms apply")]}
    target = {"pages": [page(1, "Policy Number 123", "Insured: Alpha Limited", "Premium 1000", "New clause"), page(2)]}
    d = c.diff_pdf(base, target)
    kinds = {(x["kind"], x["page"]) for x in d["changes"]}
    assert ("modified", 1) in kinds and ("added", 1) in kinds and ("removed", 2) in kinds
    mod = next(x for x in d["changes"] if x["kind"] == "modified")
    assert mod["before"] == "Insured: Alpha Ltd" and mod["after"] == "Insured: Alpha Limited" and mod["base_bbox"] and mod["target_bbox"]
    assert d["summary"] == {"added": 1, "removed": 1, "modified": 1, "total": 3, "truncated": False}


def test_pdf_identical_has_no_changes():
    snap = {"pages": [page(1, "a", "b")]}
    assert c.diff_pdf(snap, snap)["changes"] == []


def sheet(cells, styles=None, merges=()):
    return {"cells": {k: {"v": v, "t": t} for k, (v, t) in cells.items()}, "styles": styles or {}, "merges": [list(m) for m in merges], "images": [], "cols": {}, "rows": {}}


def test_xlsx_value_formula_style_merge_and_sheet_changes():
    base = {"sheets": {"S": sheet({"A1": ("10", "integer"), "B1": ("=SUM(A1:A3)", "formula"), "C1": ("x", "string")}, {"A1": {"bl": 1}}, [(1, 1, 1, 2)]), "Old": sheet({})}}
    target = {"sheets": {"S": sheet({"A1": ("12", "integer"), "B1": ("=SUM(A1:A4)", "formula"), "D1": ("new", "string")}, {"A1": {"bl": 1, "sz": 12.0}}, [(1, 1, 1, 3)]), "New": sheet({})}}
    d = c.diff_xlsx(base, target)
    by = {(x["category"], x["label"], x["kind"]) for x in d["changes"]}
    assert ("value", "S!A1", "modified") in by and ("formula", "S!B1", "modified") in by
    assert ("value", "S!C1", "removed") in by and ("value", "S!D1", "added") in by
    assert any(x["category"] == "format" and x["label"] == "S!A1" for x in d["changes"])
    assert any(x["category"] == "structure" and "merged" in x["label"] for x in d["changes"])
    assert ("structure", "Sheet Old", "removed") in by and ("structure", "Sheet New", "added") in by
    before_after = next(x for x in d["changes"] if x["label"] == "S!A1" and x["category"] == "value")
    assert (before_after["before"], before_after["after"]) == ("10", "12")


def test_xlsx_cell_order_is_by_row_then_column():
    base = {"sheets": {"S": sheet({})}}
    target = {"sheets": {"S": sheet({"B2": ("1", "integer"), "A10": ("1", "integer"), "A2": ("1", "integer")})}}
    assert [x["label"] for x in c.diff_xlsx(base, target)["changes"]] == ["S!A2", "S!B2", "S!A10"]


def block(i, text, fmt="f1", props=None, table=None):
    return {"id": f"word/document.xml:p{i}", "kind": "table_cell" if table else "paragraph", "text": text, "fmt": fmt, "props": props or {}, "table": table}


def test_docx_text_table_format_and_image_changes():
    t = {"index": 0, "row": 1, "col": 2}
    base = {"blocks": [block(0, "Heading"), block(1, "Body text"), block(2, "Cell A", table=t)], "media": [{"name": "media/image3.png", "sha256": "aa"}]}
    target = {"blocks": [block(0, "Heading", fmt="f2", props={"bold": True}), block(1, "Body text changed"), block(2, "Cell B", table=t)],
              "media": [{"name": "media/image3.png", "sha256": "bb"}, {"name": "media/image9.png", "sha256": "cc"}]}
    d = c.diff_docx(base, target)
    cats = {(x["category"], x["kind"]) for x in d["changes"]}
    assert ("format", "modified") in cats and ("text", "modified") in cats and ("image", "added") in cats and ("image", "modified") in cats
    cell = next(x for x in d["changes"] if x["location"].get("table"))
    assert cell["label"] == "Table 1, row 2, column 3" and cell["after"] == "Cell B"
    assert "images" in d["limits"][0]


def test_ids_are_stable_sequential_and_changes_capped():
    base = {"pages": [page(1, *[f"line {i}" for i in range(10)])]}
    target = {"pages": [page(1, *[f"other {i}" for i in range(10)])]}
    d = c.diff_pdf(base, target)
    assert [x["id"] for x in d["changes"]][:3] == ["c1", "c2", "c3"]
