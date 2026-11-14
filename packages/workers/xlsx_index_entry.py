"""Sandboxed worker: parse ONE workbook once and write a windowed index (per-sheet SQLite + meta.json) to a scratch directory.

Invoked as `python -m packages.workers.xlsx_index_entry --job job.json` inside RestrictedSubprocess.
job.json: {"source": path, "out": dir}
The index contains every cell the verified adapter reports (value, type, style, id, fingerprint, editability, reason) plus
styled-only cells, and the layout (merges, widths, heights, frozen panes, pictures, styles) with NO clipping: the viewer asks for
windows by sheet and row/column range, so a large worksheet is never silently truncated. stdout: one small JSON status line.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys

from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

from packages.core import xlsx_layout
from packages.core.adapters.registry import AdapterRegistry
from packages.workers.parse_entry import MAX_FILE_BYTES, _target_dict

MAX_ROWS, MAX_COLS = 1_048_576, 16_384
MAX_CELLS = 3_000_000
FORMULA_REASONS = {"formula_cell", "array_formula"}
_ADDR = re.compile(r"^[A-Z]{1,3}[0-9]{1,7}$")


def addr_to_rc(address: str) -> tuple[int, int] | None:
    if not _ADDR.match(address):
        return None
    letters, row = coordinate_from_string(address)
    col = column_index_from_string(letters)
    return (row, col) if 1 <= row <= MAX_ROWS and 1 <= col <= MAX_COLS else None


def build(data: bytes, out: str) -> dict:
    if not data or len(data) > MAX_FILE_BYTES:
        return {"error": "file_size_limit"}
    registry = AdapterRegistry()
    selection = registry.inspect(data, filename="workbook.xlsx", max_expansion_ratio=100)
    profile = selection.profile
    if profile.level == "rejected" or profile.adapter != "xlsx":
        return {"error": (profile.reasons[0] if profile.reasons else "invalid_document")}
    adapter = registry.xlsx
    layout = xlsx_layout.extract(data, max_rows=MAX_ROWS, max_cols=MAX_COLS)
    if layout is None:
        return {"error": "layout_unreadable"}
    # same ordering and de-duplication as the paged inspection (editable, then unsupported, then read-only)
    adapter_targets = adapter.targets(data)
    ordered = [t for t in adapter_targets if t.editable] + list(adapter.unsupported_targets(data)) + [t for t in adapter_targets if not t.editable]
    seen: set[str] = set()
    unique = []
    for target in ordered:
        if target.id not in seen:
            seen.add(target.id)
            unique.append(target)
    if len(unique) > MAX_CELLS:
        return {"error": "too_many_cells"}
    by_sheet: dict[str, list] = {}
    for target in unique:
        by_sheet.setdefault(target.sheet, []).append(target)
    sheets_meta = []
    styles = layout.get("styles", [])
    total_cells = 0
    for index, lay in enumerate(layout["sheets"]):
        name = lay["name"]
        db_path = os.path.join(out, f"sheet-{index}.sqlite")
        db = sqlite3.connect(db_path)
        db.execute("PRAGMA journal_mode=OFF"); db.execute("PRAGMA synchronous=OFF")
        db.execute("CREATE TABLE cells (r INTEGER, c INTEGER, v TEXT, t TEXT, s INTEGER, id TEXT, fp TEXT, e INTEGER, rs TEXT, PRIMARY KEY (r, c)) WITHOUT ROWID")
        rows_batch = []
        max_r = max_c = 0
        style_of = lay.get("cells") or {}
        have: set[tuple[int, int]] = set()
        for target in by_sheet.get(name, []):
            rc = addr_to_rc(target.cell)
            if rc is None:
                continue
            d = _target_dict(target)
            value = d.get("value") or {}
            raw = value.get("value")
            text = "" if raw is None else (str(raw).lower() if isinstance(raw, bool) else str(raw))
            rows_batch.append((rc[0], rc[1], text, value.get("type"), style_of.get(target.cell, -1), d["id"], d["fingerprint"], 1 if d.get("editable") else 0, d.get("reason")))
            have.add(rc)
            max_r, max_c = max(max_r, rc[0]), max(max_c, rc[1])
        for address, style in style_of.items():                       # styled-only cells (fills/borders on empty cells) keep their look
            rc = addr_to_rc(address)
            if rc is None or rc in have:
                continue
            rows_batch.append((rc[0], rc[1], "", None, style, "", "", 0, None))
            max_r, max_c = max(max_r, rc[0]), max(max_c, rc[1])
        db.executemany("INSERT OR REPLACE INTO cells VALUES (?,?,?,?,?,?,?,?,?)", rows_batch)
        db.commit(); db.close()
        content_col = 0                                           # last column holding a value or a locked cell: what 'fit width' should fit
        for row in rows_batch:
            if row[5] and (row[2] != "" or not row[7]):
                content_col = max(content_col, row[1])
        for r1, c1, r2, c2 in lay.get("merges", []):
            max_r, max_c = max(max_r, r2), max(max_c, c2)
            content_col = max(content_col, c2)
        total_cells += len(rows_batch)
        sheets_meta.append({"index": index, "name": name, "max_row": max_r, "max_col": max_c, "content_col": content_col or max_c, "cell_count": len(rows_batch),
                            "default_col": lay.get("default_col"), "default_row": lay.get("default_row"), "merges": lay.get("merges", []),
                            "cols": {k: w for k, w in (lay.get("cols") or {}).items() if int(k) <= max(max_c, 1)}, "rows": lay.get("rows", {}), "images": lay.get("images", []), "freeze": lay.get("freeze"),
                            "zoom": lay.get("zoom", 100), "hidden": bool(lay.get("hidden")), "protected": bool(lay.get("protected"))})
    meta = {"version": 1, "source_sha256": hashlib.sha256(data).hexdigest(), "sheets": sheets_meta, "styles": styles, "total_cells": total_cells,
            "capability": {"level": profile.level, "reasons": list(profile.reasons)}}
    with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, separators=(",", ":"), allow_nan=False)
    return {"ok": True, "sheets": len(sheets_meta), "cells": total_cells}


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--job", required=True)
    job = json.load(open(ap.parse_args().job))
    try:
        result = build(open(job["source"], "rb").read(), job["out"])
    except Exception as exc:  # noqa: BLE001 - fail closed with a stable code and no document data
        result = {"error": "xlsx_index_failed", "stage": type(exc).__name__}
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
