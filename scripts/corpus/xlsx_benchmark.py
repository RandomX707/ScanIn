"""Workbook benchmark on the ISOLATED stack: small RFQ, medium QCR, 60,000-cell and 180,000-cell synthetic workbooks.
Measures import, index (initial viewport data), window latency (scrolling), memory, edit-lookup, save, COMPLETE comparison and change coverage."""
from __future__ import annotations
import json, statistics, subprocess, sys, tempfile, time, urllib.parse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from iso_client import API as API_URL, Client  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]; CORPUS = ROOT / "Policy Copies"; PY = sys.executable
results, rows_out = [], []
def check(n, ok, d=""):
    results.append((n, bool(ok))); print(("PASS " if ok else "FAIL ") + n + (f" — {d}" if d else ""))

def editable_text_cells(window):
    """Return non-empty text cells the adapter explicitly marks editable."""
    return [cell for cell in window.get("cells", [])
            if len(cell) >= 6 and cell[5] == 1 and cell[3] == "string" and isinstance(cell[2], str) and cell[2]]

def pick_distinct_cells(top, far):
    """Select permitted non-empty text cells, allowing arbitrarily long source text."""
    near_cells = editable_text_cells(top)
    far_cells = editable_text_cells(far)
    if not near_cells or not far_cells:
        return None
    near = near_cells[0]
    far = next((cell for cell in reversed(far_cells) if cell[:2] != near[:2]), None)
    return (near, far) if far else None
def rss_mb():
    port = urllib.parse.urlsplit(API_URL).port
    pids = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True)
    pid = pids.stdout.strip().splitlines()[0] if pids.returncode == 0 and pids.stdout.strip() else None
    if not pid:
        return None
    out = subprocess.run(["ps", "-o", "rss=", "-p", pid], capture_output=True, text=True)
    try:
        return round(int(out.stdout.strip()) / 1024, 1)
    except (ValueError, TypeError):
        return None
def synthetic(rows):
    p = Path(tempfile.mkdtemp()) / f"synthetic-{rows * 30}.xlsx"; subprocess.run([PY, str(ROOT / "scripts/corpus/make_large_workbook.py"), str(rows), "30", str(p)], check=True, capture_output=True); return p

CASES = [("small RFQ workbook", next(p for p in CORPUS.rglob("*") if p.name == "CAR RFQ.xlsx")), ("medium QCR workbook", next(p for p in CORPUS.rglob("*") if p.name == "GMC QCR Format.XLSX")),
         ("60,000-cell synthetic", synthetic(2000)), ("180,000-cell synthetic", synthetic(6000))]

def main():
    c = Client()
    for label, path in CASES:
        m: dict = {"case": label}
        mem0 = rss_mb()
        t = time.time(); s, reg, insp = c.upload(path); m["import_inspect_s"] = round(time.time() - t, 2); d, v = reg["document_id"], reg["version_id"]
        check(f"{label}: import + inspect", s == 200 and insp["state"] == "ready", f"{m['import_inspect_s']}s")
        t = time.time(); s, meta, _ = c.call("GET", f"/api/documents/{d}/versions/{v}/sheet/meta?wait=20")
        while s == 202 and time.time() - t < 120: s, meta, _ = c.call("GET", f"/api/documents/{d}/versions/{v}/sheet/meta?wait=20")
        m["index_ready_s"] = round(time.time() - t, 2); check(f"{label}: windowed index ready", s == 200 and meta["state"] == "ready", f"{m['index_ready_s']}s")
        sh = meta["sheets"][0]; nm = sh["name"].replace(" ", "%20").replace(",", "%2C"); m["cells"] = meta["total_cells"]; m["rows"] = sh["max_row"]
        t = time.time(); s, w, _ = c.call("GET", f"/api/documents/{d}/versions/{v}/sheet/window?sheet={nm}&r1=1&r2=60&c1=1&c2=30"); m["first_window_ms"] = round((time.time() - t) * 1000)
        check(f"{label}: initial viewport window responds", s == 200 and bool(w.get("cells")), f"{m['first_window_ms']} ms")
        lat = []
        for frac in (0.9, 0.3, 0.6, 0.99, 0.15, 0.5, 0.75, 0.05, 0.4, 0.85):
            r1 = max(1, int(sh["max_row"] * frac) - 30)
            t = time.time(); s, w, _ = c.call("GET", f"/api/documents/{d}/versions/{v}/sheet/window?sheet={nm}&r1={r1}&r2={min(r1 + 59, sh['max_row'])}&c1=1&c2=30"); lat.append((time.time() - t) * 1000)
            assert s == 200
        m["window_p50_ms"], m["window_max_ms"] = round(statistics.median(lat)), round(max(lat))
        check(f"{label}: scrolling windows answer quickly (max {m['window_max_ms']} ms)", max(lat) < 500)
        # edit lookup + save of two text cells: one near the top, one far into the sheet
        s, wtop, _ = c.call("GET", f"/api/documents/{d}/versions/{v}/sheet/window?sheet={nm}&r1=1&r2={min(60, sh['max_row'])}&c1=1&c2=30")
        far_r1 = max(1, sh["max_row"] - 40); s, wfar, _ = c.call("GET", f"/api/documents/{d}/versions/{v}/sheet/window?sheet={nm}&r1={far_r1}&r2={sh['max_row']}&c1=1&c2=30")
        selected = pick_distinct_cells(wtop, wfar)
        check(f"{label}: permitted text edit targets exist", selected is not None,
              f"top={len(editable_text_cells(wtop))}, far={len(editable_text_cells(wfar))}")
        if selected is None:
            rows_out.append(m)
            continue
        near, far = selected
        m["edit_targets"] = [{"row": cell[0], "column": cell[1], "eligible": bool(cell[5]),
                              "type": cell[3], "source_length": len(cell[2])} for cell in (near, far)]
        t = time.time(); details = []
        for cell in (near, far):
            s, dt, _ = c.call("GET", f"/api/documents/{d}/versions/{v}/sheet/cell?sheet={nm}&row={cell[0]}&col={cell[1]}"); assert s == 200; details.append(dt)
        m["edit_lookup_ms"] = round((time.time() - t) * 1000 / 2)
        changes = [{"target_id": dt["id"], "fingerprint": dt["fp"], "value": dt["v"] + " edited"} for dt in details]
        t = time.time(); s, body, _ = c.post(f"/api/documents/{d}/versions/{v}/save", {"changes": changes}); m["save_s"] = round(time.time() - t, 2)
        check(f"{label}: native save within the 30 s worker budget", s == 200 and body["outcome"] in ("VERIFIED", "INCOMPLETE") and m["save_s"] < 30, f"{m['save_s']}s {body.get('outcome') or body.get('code')}")
        v2 = body["version_id"]
        fs, saved_bytes, _ = c.call("GET", f"/api/documents/{d}/versions/{v2}/file", raw=True)
        if body.get("outcome") == "INCOMPLETE":
            m["native_reopen"] = None
            check(f"{label}: incomplete save remains withheld by download policy", fs == 409, f"download={fs}")
        else:
            try:
                import openpyxl
                saved_book = openpyxl.load_workbook(__import__("io").BytesIO(saved_bytes), data_only=False) if fs == 200 else None
                m["native_reopen"] = bool(saved_book and sh["name"] in saved_book.sheetnames and
                                           all(saved_book[sh["name"]][cell["id"].split("!")[-1]].value == change["value"]
                                               for cell, change in zip(details, changes)))
            except Exception as exc:  # noqa: BLE001
                m["native_reopen"] = False
                m["native_reopen_error"] = type(exc).__name__
            check(f"{label}: saved file independently reopens with both edits", m["native_reopen"], f"download={fs}")
        t = time.time(); s, cmp, _ = c.call("GET", f"/api/documents/{d}/compare?base={v}&target={v2}&wait=20")
        while s == 202 and time.time() - t < 240: s, cmp, _ = c.call("GET", f"/api/documents/{d}/compare?base={v}&target={v2}&wait=20")
        m["compare_s"] = round(time.time() - t, 2)
        got = sorted(x["label"] for x in cmp["changes"]) if s == 200 else []
        want = sorted(f"{sh['name']}!{dt['id'].split('!')[-1]}" for dt in details)
        beyond = far[0] > 60
        m["far_target_outside_initial_viewport"] = beyond
        m["compare_complete"] = bool(s == 200 and cmp.get("complete")); m["changes_found"], m["changes_expected"], m["examined_cells"] = len(got), len(want), cmp.get("examined_cells")
        check(f"{label}: COMPLETE comparison finds every edit{' (including cells beyond position 16,000)' if far[0] * 30 > 16000 else ''}", s == 200 and cmp["complete"] and got == want and cmp["summary"]["total"] == len(want),
              f"{m['compare_s']}s, examined {cmp.get('examined_cells')} cells, found {got}")
        m["api_rss_mb_before_after"] = [mem0, rss_mb()]
        rows_out.append(m)
    bad = [n for n, ok in results if not ok]
    out = ROOT / ".private/feature-results/xlsx-benchmark.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "api": API_URL,
        "database": __import__("os").environ.get("C360_ISO_DB", "channel360_iso_dev"),
        "git_head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip(),
        "source_sha256": subprocess.run([PY, "-c", "import hashlib,pathlib; p=pathlib.Path('packages/core/adapters/xlsx/adapter.py'); print(hashlib.sha256(p.read_bytes()).hexdigest())"], cwd=ROOT, capture_output=True, text=True).stdout.strip(),
        "checks": [{"check": name, "pass": ok} for name, ok in results],
        "cases": rows_out,
    }
    out.write_text(json.dumps(metadata, indent=1))
    print("\nBENCH", json.dumps(rows_out)); print(f"{len(results) - len(bad)} passed, {len(bad)} failed; results: {out}"); sys.exit(1 if bad else 0)

if __name__ == "__main__":
    main()
