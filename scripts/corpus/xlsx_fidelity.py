"""Feature-B check: our spreadsheet layout model vs an INDEPENDENT parse (openpyxl) of the same real workbooks. Counts only."""
from __future__ import annotations
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from iso_client import Client  # noqa: E402
import openpyxl  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]; CORPUS = ROOT / "Policy Copies"
results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail))); print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else "")); return ok

def unique_books():
    seen, out = set(), []
    for p in sorted(CORPUS.rglob("*")):
        if p.suffix.lower() == ".xlsx":
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            if h not in seen: seen.add(h); out.append(p)
    return out

def independent(path):
    wb = openpyxl.load_workbook(path)
    sheets = {}
    for ws in wb.worksheets:
        cols = {}
        for key, dim in ws.column_dimensions.items():
            lo, hi = (dim.min or 0), (dim.max or 0)
            for i in range(max(lo, 1), min(hi, 40) + 1):
                cols[i] = 0.0 if dim.hidden else float(dim.width or 0)
        rows = {r: (0.0 if d.hidden else float(d.height)) for r, d in ws.row_dimensions.items() if (d.hidden or d.height) and r <= 500}
        merges = {(m.min_row, m.min_col, min(m.max_row, 500), min(m.max_col, 40)) for m in ws.merged_cells.ranges if m.min_row <= 500 and m.min_col <= 40}
        images = sorted((i.anchor._from.row, i.anchor._from.col) for i in ws._images)
        formats = {c.coordinate: c.number_format for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 500)) for c in row if c.number_format != "General" and c.value is not None}
        sheets[ws.title] = {"cols": cols, "rows": rows, "merges": merges, "images": images, "formats": formats, "state": ws.sheet_state,
                            "freeze": ws.freeze_panes, "protected": bool(ws.protection.sheet), "max_row": ws.max_row, "max_col": ws.max_column}
    return sheets

def main():
    c = Client(); books = unique_books()
    check("nine distinct stakeholder workbooks found", len(books) == 9, len(books))
    for path in books:
        tag = path.name[:22]
        ind = independent(path)
        s, reg, _ = c.upload(path); d, v = reg["document_id"], reg["version_id"]
        pv = c.json(f"/api/documents/{d}/versions/{v}/preview"); styles = pv["styles"]
        api = {s_["name"]: s_ for s_ in pv["sheets"]}
        check(f"{tag}: all worksheet names present", set(api) == set(ind), f"{len(api)}/{len(ind)}")
        bad = {"merges": 0, "cols": 0, "rows": 0, "images": 0, "freeze": 0, "hidden_sheet": 0, "protected": 0, "numfmt": 0, "dims": 0}; fmt_checked = 0
        for name, a in api.items():
            L, I = a["layout"], ind[name]
            if {tuple(m) for m in L["merges"]} != I["merges"]: bad["merges"] += 1
            for i, w in I["cols"].items():
                if abs(float(L["cols"].get(str(i), -1)) - w) > 0.01 and not (w == 0 and L["cols"].get(str(i)) == 0): bad["cols"] += 1; break
            for r, h in I["rows"].items():
                if abs(float(L["rows"].get(str(r), -1)) - h) > 0.01: bad["rows"] += 1; break
            if sorted((i["from"]["row"], i["from"]["col"]) for i in L["images"]) != I["images"]: bad["images"] += 1
            exp_freeze = None if not I["freeze"] else I["freeze"]
            got = L.get("freeze"); got_cell = (get_column_letter((got["cols"] or 0) + 1) + str((got["rows"] or 0) + 1)) if got else None
            if (got_cell or None) != exp_freeze: bad["freeze"] += 1
            if bool(L.get("hidden")) != (I["state"] != "visible"): bad["hidden_sheet"] += 1
            if bool(L.get("protected")) != I["protected"]: bad["protected"] += 1
            for addr, fmt in list(I["formats"].items())[:80]:
                idx = L["cells"].get(addr)
                got_fmt = styles[idx].get("nf") if idx is not None and idx < len(styles) else None
                fmt_checked += 1
                if (got_fmt or "General") != fmt: bad["numfmt"] += 1
            rows_n = len(a["rows"])
            if rows_n < min(I["max_row"], 500) - 0 and a["rows"]: bad["dims"] += 1 if rows_n + 1 < min(I["max_row"], 500) else 0
        for k, n in bad.items():
            check(f"{tag}: {k} match the independent parse", n == 0, f"{n} sheet(s) differ" if n else (f"{fmt_checked} formats checked" if k == "numfmt" else ""))
    failed = [r for r in results if not r[1]]
    (ROOT / ".private/feature-results/xlsx-fidelity.json").write_text(json.dumps([{"check": n, "pass": ok} for n, ok, _ in results], indent=1))
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed"); sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
