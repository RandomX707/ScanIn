"""Cell-anchored template understanding (synthetic)."""
from packages.core.sheet_sections import docx_chunks, sheet_chunks


def C(v, e=True, t="string"):
    return {"v": v, "t": t, "id": "x", "fp": "f", "e": e, "r": None}


SHEET = {"name": "RFQ", "layout": {"merges": [[1, 1, 1, 4], [5, 1, 5, 4]], "cells": {"A1": 0, "A5": 0}},
         "rows": [
             [C("Request for Quotation"), None, None, None],
             [C("Insured name"), C("", t="null"), C("Policy period"), C("1 year")],
             [C("Address"), C("", t="null"), None, None],
             [None, None, None, None],
             [C("COVERAGE REQUIRED"), None, None, None],
             [C("Sum insured"), C("5000000", t="integer"), C("Deductible"), C("", t="null")],
             [C("Total premium"), C("=B6*0.01", e=False), None, None],
         ]}
STYLES = [{"b": 1, "bg": "#DDEEFF"}]


def test_sections_have_cell_anchored_fields_and_honest_blanks():
    chunks = sheet_chunks(SHEET, STYLES)
    assert [c["title"] for c in chunks] == ["Request for Quotation", "COVERAGE REQUIRED"]
    first, second = chunks
    f = {x["label"]: x for x in first["facts"]}
    assert f["Insured name"]["blank"] and f["Insured name"]["cell"] == "B2" and f["Insured name"]["label_cell"] == "A2"
    assert f["Policy period"]["value"] == "1 year" and f["Policy period"]["cell"] == "D2"
    assert "awaiting input" in first["summary"] and first["source"]["label"] == "RFQ · rows 1–3"
    g = {x["label"]: x for x in second["facts"]}
    assert g["Sum insured"]["value"] == "5000000" and g["Deductible"]["blank"] and g["Total premium"]["formula"]
    assert any("Awaiting input" in p for p in second["points"]) or any("5000000" in p for p in second["points"])


def test_no_value_is_ever_invented_for_blank_fields():
    for ch in sheet_chunks(SHEET, STYLES):
        for fact in ch["facts"]:
            if fact["blank"]:
                assert fact["value"] == ""


def test_docx_table_rows_become_sections_and_facts():
    blocks = [
        {"kind": "paragraph", "text": "Health Quotation", "heading": None, "table": None},
        {"kind": "table_cell", "text": "PROPOSER DETAILS", "heading": None, "table": {"index": 0, "row": 0, "col": 0}},
        {"kind": "table_cell", "text": "Name", "heading": None, "table": {"index": 0, "row": 1, "col": 0}},
        {"kind": "table_cell", "text": "A. Example", "heading": None, "table": {"index": 0, "row": 1, "col": 1}},
        {"kind": "table_cell", "text": "Age", "heading": None, "table": {"index": 0, "row": 2, "col": 0}},
        {"kind": "table_cell", "text": "41", "heading": None, "table": {"index": 0, "row": 2, "col": 1}},
    ]
    chunks = docx_chunks(blocks)
    titles = [c["title"] for c in chunks]
    assert "PROPOSER DETAILS" in titles
    sec = next(c for c in chunks if c["title"] == "PROPOSER DETAILS")
    assert {(f["label"], f["value"]) for f in sec["facts"]} == {("Name", "A. Example"), ("Age", "41")}
    assert sec["facts"][0]["cell"].startswith("table 1, row")
