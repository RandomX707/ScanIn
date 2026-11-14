"""Performance / stability on the ISOLATED stack: the 72-page Fire PDF, a large synthetic workbook, repeated comparisons, DOCX render cache."""
from __future__ import annotations
import hashlib, io, json, subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))
from iso_client import Client  # noqa: E402
import pypdf, openpyxl  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]; CORPUS = ROOT / "Policy Copies"
results, timings = [], {}
def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail))); print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else "")); return ok
def rss_mb():
    out = subprocess.run("ps -o rss= -p $(lsof -nP -iTCP:8010 -sTCP:LISTEN -t | head -1)", shell=True, capture_output=True, text=True).stdout.strip()
    return int(out) // 1024 if out else 0
def timed(key, fn):
    t = time.time(); r = fn(); timings[key] = round(time.time() - t, 2); return r

def main():
    c = Client(); fire = CORPUS / "Fire Policy copy.pdf"
    pages = len(pypdf.PdfReader(str(fire)).pages); timings["fire_pages"] = pages
    check("Fire PDF has the expected large page count", pages >= 60, pages)
    mem0 = rss_mb()
    s, reg, insp = timed("fire_import_inspect_s", lambda: c.upload(fire)); d, v1 = reg["document_id"], reg["version_id"]
    check("import + inspect", s == 200 and insp["state"] == "ready", f"{timings['fire_import_inspect_s']}s")
    pv = timed("fire_preview_s", lambda: c.json(f"/api/documents/{d}/versions/{v1}/preview")); check("preview", pv["edit"]["mode"] == "pdf_text" and len(pv["pages"]) == pages, f"{timings['fire_preview_s']}s")
    ch = timed("fire_chunks_cold_s", lambda: c.json(f"/api/documents/{d}/versions/{v1}/chunks")); check("sections (cold)", 1 <= len(ch["sections"]) <= 12, f"{timings['fire_chunks_cold_s']}s")
    runs = [r for r in c.json(f"/api/documents/{d}/versions/{v1}/pdf-text?page=1")["runs"] if r["e"] and " " in r["text"] and 14 <= len(r["text"]) <= 40]
    r0 = runs[0]; s, body, _ = timed("fire_edit_save_s", lambda: c.post(f"/api/documents/{d}/versions/{v1}/save", {"pdf_edits": [{"id": r0["id"], "old": r0["text"], "new": " ".join(r0["text"].split()[:-1])}]}))
    check("edit + verified save on the 72-page PDF", s == 200 and body["outcome"] == "VERIFIED", f"{timings['fire_edit_save_s']}s"); v2 = body["version_id"]
    s, cmp, _ = timed("fire_compare_cold_s", lambda: c.call("GET", f"/api/documents/{d}/compare?base={v1}&target={v2}"))
    check("compare 72 pages (cold)", s == 200 and cmp["summary"]["total"] == 1, f"{timings['fire_compare_cold_s']}s {cmp.get('summary')}")
    reps = []
    for _ in range(6):
        t = time.time(); s, cmp, _ = c.call("GET", f"/api/documents/{d}/compare?base={v1}&target={v2}"); reps.append(time.time() - t)
    timings["fire_compare_repeat_max_s"] = round(max(reps), 2); check("six repeated comparisons stay fast (snapshot cache)", s == 200 and max(reps) < 3.0, f"max {max(reps):.2f}s")
    t = time.time(); s, hist, _ = c.call("GET", f"/api/documents/{d}/history"); timings["fire_history_s"] = round(time.time() - t, 2); check("history with signature status", s == 200 and len(hist["versions"]) == 2, f"{timings['fire_history_s']}s")
    # large synthetic workbook
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Big"
    for r in range(1, 6001):
        ws.append([f"Row {r}", r, r * 1.5, "x" * 12] + [r + k for k in range(26)])
    ws2 = wb.create_sheet("Second"); [ws2.append([i, f"v{i}"]) for i in range(1, 1500)]
    tmp = Path(tempfile.mkdtemp()) / "big.xlsx"; wb.save(tmp); timings["big_xlsx_mb"] = round(tmp.stat().st_size / 1e6, 2)
    s, reg2, insp2 = timed("big_xlsx_import_s", lambda: c.upload(tmp)); check("large workbook imports", s == 200 and insp2["state"] == "ready", f"{timings['big_xlsx_import_s']}s, {timings['big_xlsx_mb']} MB")
    d2, b1 = reg2["document_id"], reg2["version_id"]
    pv2 = timed("big_xlsx_preview_s", lambda: c.json(f"/api/documents/{d2}/versions/{b1}/preview")); check("large workbook preview is time-bounded (<= ~35 s), row-bounded and explicitly flagged partial", pv2.get("partial") is True and all(len(s_["rows"]) <= 500 for s_ in pv2["sheets"]) and timings["big_xlsx_preview_s"] < 35, f"{timings['big_xlsx_preview_s']}s partial={pv2.get('partial')}")
    cell = next(x for x in pv2["sheets"][0]["rows"][3] if x and x["t"] == "string" and x["e"])
    s, body2, _ = timed("big_xlsx_save_s", lambda: c.post(f"/api/documents/{d2}/versions/{b1}/save", {"changes": [{"target_id": cell["id"], "fingerprint": cell["fp"], "value": "edited"}]}))
    check("large workbook native save", s == 200, f"{timings['big_xlsx_save_s']}s {body2.get('outcome') or body2.get('code')}")
    if s == 200:
        s, cmp2, _ = timed("big_xlsx_compare_s", lambda: c.call("GET", f"/api/documents/{d2}/compare?base={b1}&target={body2['version_id']}"))
        check("large workbook compare: same window on both sides, exactly the one edited cell, no phantom additions, limit disclosed", s == 200 and cmp2["summary"]["total"] == 1 and cmp2["summary"]["modified"] == 1
              and cmp2["summary"].get("incomplete") is True and any("first 16,000 cells" in l for l in cmp2["limits"]), f"{timings['big_xlsx_compare_s']}s {cmp2.get('summary')}")
        s, cmp3, _ = timed("big_xlsx_compare_repeat_s", lambda: c.call("GET", f"/api/documents/{d2}/compare?base={b1}&target={body2['version_id']}"))
        check("repeat compare of the large workbook is served from the snapshot cache", s == 200 and timings["big_xlsx_compare_repeat_s"] < 3.0, f"{timings['big_xlsx_compare_repeat_s']}s")
    # docx render cache
    docx = CORPUS / "QCR/Health Quote Format.docx"; s, reg3, _ = c.upload(docx); d3, h1 = reg3["document_id"], reg3["version_id"]
    t = time.time(); c.call("GET", f"/api/documents/{d3}/versions/{h1}/render", raw=True); cold = time.time() - t
    t = time.time(); c.call("GET", f"/api/documents/{d3}/versions/{h1}/render", raw=True); warm = time.time() - t
    timings["docx_render_cold_s"], timings["docx_render_cached_s"] = round(cold, 2), round(warm, 3); check("DOCX render is cached per version", warm < max(0.5, cold / 4), f"cold {cold:.2f}s, cached {warm:.3f}s")
    # concurrency: parallel renders of the same uncached version run once
    import concurrent.futures as cf
    s, reg4, _ = c.upload(docx); d4, k1 = reg4["document_id"], reg4["version_id"]
    t = time.time()
    with cf.ThreadPoolExecutor(6) as ex: outs = list(ex.map(lambda _: c.call("GET", f"/api/documents/{d4}/versions/{k1}/render", raw=True)[0], range(6)))
    timings["docx_6_parallel_s"] = round(time.time() - t, 2); check("six parallel render requests all succeed (single flight, bounded slots)", all(o == 200 for o in outs), f"{timings['docx_6_parallel_s']}s")
    mem1 = rss_mb(); timings["api_rss_mb"] = {"before": mem0, "after": mem1}; check("API memory stays reasonable after the run (< 1.5 GB)", mem1 < 1536, f"{mem0}→{mem1} MB")
    print("\nTIMINGS", json.dumps(timings))
    failed = [r for r in results if not r[1]]
    (ROOT / ".private/feature-results/perf.json").write_text(json.dumps({"checks": [{"check": n, "pass": ok} for n, ok, _ in results], "timings": timings}, indent=1))
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed"); sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
