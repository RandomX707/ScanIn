"""The windowed index must lose NOTHING: every non-empty cell, formula, merge, size, frozen pane and picture of the 9 real workbooks, checked
against an independent openpyxl parse, reading the data back through the public window API in blocks. Counts only."""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from iso_client import Client  # noqa: E402
import openpyxl  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]; CORPUS = ROOT / "Policy Copies"
results = []
def check(n, ok, d=""):
    results.append((n, bool(ok))); print(("PASS " if ok else "FAIL ") + n + (f" — {d}" if d else ""))

def norm(v):
    if v is None: return ""
    if isinstance(v, bool): return str(v).lower()
    return str(v)

def main():
    c = Client(); seen = set()
    for path in sorted(CORPUS.rglob("*")):
        if path.suffix.lower() != ".xlsx": continue
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        if h in seen: continue
        seen.add(h); tag = path.name[:24]
        wb = openpyxl.load_workbook(path)                          # formulas as written
        s, reg, _ = c.upload(path); d, v = reg["document_id"], reg["version_id"]
        s, meta, _ = c.call("GET", f"/api/documents/{d}/versions/{v}/sheet/meta?wait=20")
        check(f"{tag}: index ready", s == 200 and meta.get("state") == "ready", s)
        if s != 200: continue
        by_name = {x["name"]: x for x in meta["sheets"]}
        missing_cells = extra_cells = wrong_values = merge_diff = pic_diff = size_diff = freeze_diff = 0; compared = 0; formulas = 0
        for ws in wb.worksheets:
            sh = by_name.get(ws.title)
            if sh is None: missing_cells += 1; continue
            got: dict[tuple[int, int], tuple[str, int]] = {}
            for br in range(1, max(sh["max_row"], 1) + 1, 150):
                r2 = min(br + 149, sh["max_row"])
                for bc in range(1, max(sh["max_col"], 1) + 1, 60):
                    c2 = min(bc + 59, sh["max_col"])
                    s2, w, _ = c.call("GET", f"/api/documents/{d}/versions/{v}/sheet/window?sheet={ws.title.replace(' ', '%20').replace(',', '%2C')}&r1={br}&r2={r2}&c1={bc}&c2={c2}")
                    assert s2 == 200, (s2, w)
                    for r, cc, val, t, st, e, rs in w["cells"]: got[(r, cc)] = (val, e)
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value is None: continue
                    compared += 1
                    expected = norm(cell.value)
                    g = got.get((cell.row, cell.column))
                    if g is None: missing_cells += 1; continue
                    if isinstance(cell.value, str) and cell.value.startswith("="): formulas += 1
                    # numbers and dates are typed by the adapter (canonical text); strings and formulas must match exactly
                    if isinstance(cell.value, str) and g[0] != expected: wrong_values += 1
            merged = {(m.min_row, m.min_col, m.max_row, m.max_col) for m in ws.merged_cells.ranges}
            if {tuple(m) for m in sh["merges"]} != merged: merge_diff += 1
            if sorted((i.anchor._from.row, i.anchor._from.col) for i in ws._images) != sorted((i["from"]["row"], i["from"]["col"]) for i in sh["images"]): pic_diff += 1
            for k, dim in ws.column_dimensions.items():
                for i in range(dim.min or 0, (dim.max or 0) + 1):
                    if 1 <= i <= sh["max_col"] and not dim.hidden and abs(float(sh["cols"].get(str(i), -1)) - float(dim.width or 0)) > 0.01 and dim.width: size_diff += 1; break
            exp_freeze = ws.freeze_panes
            f = sh.get("freeze"); got_freeze = (get_column_letter(f["cols"] + 1) + str(f["rows"] + 1)) if f else None
            if (got_freeze or None) != exp_freeze: freeze_diff += 1
            if ws.max_row > sh["max_row"] or ws.max_column > sh["max_col"]: missing_cells += 1
        check(f"{tag}: every non-empty cell is present in the index ({compared} cells, {formulas} formulas)", missing_cells == 0, f"{missing_cells} missing")
        check(f"{tag}: strings and formulas are identical to the file", wrong_values == 0, f"{wrong_values} differ")
        check(f"{tag}: merges, pictures, column widths and frozen panes match", merge_diff == pic_diff == size_diff == freeze_diff == 0, f"merge {merge_diff} pic {pic_diff} width {size_diff} freeze {freeze_diff}")
    bad = [n for n, ok in results if not ok]
    (ROOT / ".private/feature-results/xlsx-index-fidelity.json").write_text(json.dumps([{"check": n, "pass": ok} for n, ok in results], indent=1))
    print(f"\n{len(results) - len(bad)} passed, {len(bad)} failed"); sys.exit(1 if bad else 0)

if __name__ == "__main__":
    main()
