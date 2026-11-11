#!/usr/bin/env python3
"""Open a downloaded version with its matching library and assert it contains the expected change.

Usage: verify_saved_download.py <kind> <path> <expected> [field-name]
"""
from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path


def contains_value(node, expected) -> bool:
    if isinstance(node, dict):
        return any(contains_value(v, expected) for v in node.values())
    if isinstance(node, list):
        return any(contains_value(v, expected) for v in node)
    return str(node) == expected


def main() -> int:
    kind, path, expected = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    data = path.read_bytes()
    if kind in {"txt", "md", "html"}:
        ok = expected in data.decode("utf-8") and (kind != "html" or "<title>" in data.decode("utf-8"))
    elif kind == "csv":
        ok = any(expected in row for row in csv.reader(io.StringIO(data.decode("utf-8"))))
    elif kind == "json":
        ok = contains_value(json.loads(data.decode("utf-8")), expected)
    elif kind == "xlsx":
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
        ok = any(str(cell.value) == expected or (isinstance(cell.value, float) and f"{cell.value:g}" == expected)
                 for sheet in workbook for row in sheet.iter_rows() for cell in row)
    elif kind == "docx":
        import docx
        document = docx.Document(io.BytesIO(data))
        texts = [p.text for p in document.paragraphs] + [c.text for t in document.tables for r in t.rows for c in r.cells]
        ok = any(expected in text for text in texts)
    elif kind == "pdf":
        from pypdf import PdfReader
        fields = PdfReader(io.BytesIO(data)).get_fields() or {}
        ok = str(fields[sys.argv[4]].get("/V")) == expected
    else:
        raise SystemExit(f"unknown kind {kind}")
    print("OK" if ok else "MISSING")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
