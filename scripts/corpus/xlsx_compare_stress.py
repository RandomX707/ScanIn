"""F3 verification: complete comparison with a very large number of changes, bounded retained list and bounded memory.
Builds two real windowed indexes (sandboxed indexer) from SYNTHETIC workbooks and runs the production comparison engine in a clean process.
Counts and memory only. No server, database or stakeholder data involved."""
from __future__ import annotations
import json, resource, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT)); PY = sys.executable
import openpyxl  # noqa: E402
from packages.core import compare as engine  # noqa: E402
from packages.workers.xlsx_index import RestrictedXlsxIndexer  # noqa: E402
from packages.api.sheet_index import load_meta, open_sheet  # noqa: E402
results = []
def check(n, ok, d=""):
    results.append(bool(ok)); print(("PASS " if ok else "FAIL ") + n + (f" — {d}" if d else ""))
def rss_mb(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // (1024 * 1024)      # macOS reports bytes

def variant(base: Path, out: Path, *, text_cols: int, formula_every: int, everything: bool = False):
    wb = openpyxl.load_workbook(base); ws = wb.active
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for cell in row[:text_cols]:
            if isinstance(cell.value, str) and not cell.value.startswith("="): cell.value += " ~"
        if everything:
            for cell in row:
                if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool): cell.value = cell.value + 1
    formulas = [c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    for c in (formulas[::formula_every] if formula_every else []): c.value = c.value.replace("=", "=1*", 1)
    ws.cell(row=ws.max_row, column=1).value = "tail edit"
    wb.save(out)
    # independent oracle: compare the two native files cell by cell with openpyxl
    a, b = openpyxl.load_workbook(base).active, openpyxl.load_workbook(out).active
    cells = {(r, c) for sheet in (a, b) for r, c in ((cell.row, cell.column) for row in sheet.iter_rows() for cell in row if cell.value is not None)}
    changed = sum(1 for r, c in cells if a.cell(row=r, column=c).value != b.cell(row=r, column=c).value)
    return changed, len(formulas[::formula_every] if formula_every else [])

def run(label, rows, text_cols, formula_every, everything=False):
    work = Path(tempfile.mkdtemp()); base, target = work / "base.xlsx", work / "target.xlsx"
    subprocess.run([PY, str(ROOT / "scripts/corpus/make_large_workbook.py"), str(rows), "30", str(base)], check=True, capture_output=True)
    expected, formulas = variant(base, target, text_cols=text_cols, formula_every=formula_every, everything=everything)
    indexer = RestrictedXlsxIndexer(ROOT); da, db = work / "ia", work / "ib"; indexer.build(base, da); indexer.build(target, db)
    child = subprocess.run([PY, __file__, "child", str(da), str(db)], capture_output=True, text=True, cwd=ROOT)       # fresh process: its own high-water mark
    out = json.loads(child.stdout.strip().splitlines()[-1]); result = out["result"]; baseline, peak = out["baseline_mb"], out["peak_mb"]; s = result["summary"]
    detected = result["total_changes_detected"]; kept = result["returned_changes"]
    check(f"{label}: full scan COMPLETE over {result['examined_cells']} cells", result["scan_status"] == "COMPLETE" and result["complete"] and result["examined_cells"] >= rows * 30)
    check(f"{label}: exact counts — detected {detected} == expected {expected}", detected == expected and s["total"] == expected and s["added"] + s["removed"] + s["modified"] == expected)
    check(f"{label}: no phantom additions/removals", s["added"] == 0 and s["removed"] == 0, f"modified={s['modified']}")
    check(f"{label}: retained list bounded (<= 16 MB, <= 200k) and honestly flagged", result["retained_change_bytes"] <= 16 * 1024 * 1024 and kept <= engine.MAX_CHANGES and
          (result["list_status"] == "TRUNCATED") == (kept < detected) and result["has_more"] == (kept < detected), f"kept {kept} of {detected}, {result['retained_change_bytes'] // 1024} KiB, list {result['list_status']}")
    check(f"{label}: memory bounded — peak {peak} MB (growth {peak - baseline} MB during the scan)", peak - baseline < 160, f"{formulas} formula edits")
    print("RESULT", json.dumps({"case": label, "cells": rows * 30, "changes": detected, "returned": kept, "list": result["list_status"], "peak_mb": peak, "growth_mb": peak - baseline}))

def child(da, db):
    da, db = Path(da), Path(db)
    ma, mb = load_meta(da), load_meta(db); baseline = rss_mb()
    result = engine.diff_xlsx_indexed(ma, mb, lambda i: open_sheet(da, i), lambda i: open_sheet(db, i))
    print(json.dumps({"baseline_mb": baseline, "peak_mb": rss_mb(), "result": {k: v for k, v in result.items() if k != "changes"} | {"changes_len": len(result["changes"])}}))

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "child":
    child(sys.argv[2], sys.argv[3]); sys.exit(0)
if __name__ == "__main__":
    run("small (2k cells)", 66, 3, 5)
    run("60,000 cells, ~40k changes", 2000, 21, 25)
    run("180,000 cells, ~120k changes", 6000, 21, 50)
    run("180,000 cells, every value cell changed (forces truncation)", 6000, 30, 50, everything=True)
    print(f"{sum(results)} passed, {len(results) - sum(results)} failed"); sys.exit(0 if all(results) else 1)
