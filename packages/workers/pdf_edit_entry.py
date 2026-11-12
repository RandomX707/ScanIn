"""Sandboxed worker: list editable text runs of a page, or apply text edits and verify the result.

Invoked as `python -m packages.workers.pdf_edit_entry --job job.json` inside RestrictedSubprocess.
job.json: {"mode": "runs"|"edit", "source": path, "page": int, "edits": [{"id","old","new"}], "out": path}
stdout: one JSON document.  Never prints document text except in the `runs` listing the caller asked for.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys

import numpy as np
import pypdfium2 as pdfium
from pypdf import PdfReader

from packages.core.pdf_text import EditRefused, PdfTextEngine, edit_order

MAX_EDITS = 200
PIXEL_THRESHOLD = 10
PAD_PT = 3.0


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _lines(s: str) -> list[str]:
    """Normalised text lines. Absence checks work per line so a match can't straddle two different lines."""
    return [_norm(line) for line in re.split(r"[\r\n]+", s) if line.strip()]


def _visible_runs(engine: PdfTextEngine, source: bytes, page: int) -> list:
    """Runs that really draw something on the page (off-page, clipped and covered text is dropped)."""
    scale = 2.0
    img = _render(source, page, scale)
    l, b, _r, t = engine.page_box(page)
    out = []
    for r in engine.runs(page):
        if not engine.is_visible(r):
            continue
        x0, y0, x1, y1 = r.bbox
        box = img[max(int((t - y1) * scale), 0):max(int((t - y0) * scale), 1), max(int((x0 - l) * scale), 0):max(int((x1 - l) * scale), 1)]
        if box.size == 0 or not (np.abs(box - np.bincount(np.clip(box.ravel(), 0, 255).astype(np.int64)).argmax()) > 30).any():
            continue
        out.append(r)
    return out


def outline(source: bytes) -> dict:
    """Visible text of the whole document as lines with page, position, size and weight (for section understanding)."""
    engine = PdfTextEngine(source)
    pages = []
    for page in range(engine.page_count):
        pg = engine.writer.pages[page]
        height = float(engine.page_box(page)[3])
        runs = sorted(_visible_runs(engine, source, page), key=lambda r: (-round(r.y / 2.0), r.x0))
        lines: list[dict] = []
        for r in runs:
            text = r.text.strip()
            if not text:
                continue
            bold = bool(re.search(r"bold|black|heavy|semibold|demi", r.font_name, re.I))
            if lines and abs(lines[-1]["y0"] - r.y) <= 1.6 and r.x0 >= lines[-1]["x1"] - 1:
                ln = lines[-1]
                gap = r.x0 - ln["x1"]
                if gap > 0.6 * r.size:                       # a cell/column boundary: keep it as its own segment
                    ln["segs"].append({"x": round(r.x0, 1), "t": text})
                else:
                    ln["segs"][-1]["t"] += (" " if gap > 0.12 * r.size else "") + text
                ln["x1"] = r.x1; ln["s"] = max(ln["s"], r.size); ln["b"] = ln["b"] and bold; ln["n"] += 1
            else:
                lines.append({"segs": [{"x": round(r.x0, 1), "t": text}], "x": round(r.x0, 1), "x1": r.x1, "y0": r.y,
                              "y": round(height - r.y, 1), "s": round(r.size, 1), "b": bold, "n": 1})
        for ln in lines:
            ln["t"] = " ".join(seg["t"] for seg in ln["segs"])
            ln.pop("x1"); ln.pop("y0"); ln["s"] = round(ln["s"], 1)
        pages.append({"page": page + 1, "width": float(pg.mediabox.width), "height": float(pg.mediabox.height), "lines": lines})
    return {"pages": pages}


def list_runs(source: bytes, page: int) -> dict:
    engine = PdfTextEngine(source)
    if not 1 <= page <= engine.page_count:
        return {"error": "page_out_of_range"}
    pg = engine.writer.pages[page - 1]
    runs = []
    for r in _visible_runs(engine, source, page - 1):
        ok, why = engine.run_status(r)
        runs.append({**r.public(), "e": ok, "r": why})
    return {"page": page, "width": float(pg.mediabox.width), "height": float(pg.mediabox.height), "runs": runs}


def _render(data: bytes, index: int, scale: float) -> np.ndarray:
    doc = pdfium.PdfDocument(data)
    try:
        return np.asarray(doc[index].render(scale=scale).to_pil().convert("L"), dtype=np.int16)
    finally:
        doc.close()


def apply_edits(source: bytes, edits: list[dict]) -> dict:
    if not edits or len(edits) > MAX_EDITS:
        return {"error": "edit_count_invalid"}
    engine = PdfTextEngine(source)
    before_runs: dict[int, list] = {}
    by_id = {e["id"]: e for e in edits}
    if len(by_id) != len(edits):
        return {"error": "duplicate_target"}
    plans = []
    for rid in edit_order(list(by_id)):
        e = by_id[rid]
        run = engine.find(rid)
        before_runs.setdefault(run.page, [r for r in engine.runs(run.page)])
        try:
            res = engine.replace(rid, e["new"], expect_old=e["old"])
        except EditRefused as err:
            return {"error": err.code, "message": err.message, "edit_id": rid}
        plans.append({"id": rid, "page": run.page, "old": e["old"], "new": e["new"], "bbox": list(run.bbox),
                      "x0": run.x0, "y": run.y, "new_width": res["new_width"], "size": run.size, "font": run.font_name,
                      "substitute_font": res.get("substitute_font"), "cells": res["cells"]})
    out = engine.save()
    return {"out": out, "plans": plans, "before_runs": before_runs, "page_count": engine.page_count}


def verify(source: bytes, out: bytes, plans: list[dict], before_runs: dict[int, list]) -> dict:
    checks: list[dict] = []

    def add(name, ok, detail, critical=True):
        checks.append({"check": name, "status": "pass" if ok else "fail", "critical": critical, "detail": detail})

    # 1. independent re-open
    try:
        reader = PdfReader(io.BytesIO(out), strict=False)
        src_reader = PdfReader(io.BytesIO(source), strict=False)
        pdoc = pdfium.PdfDocument(out)
        n_out, n_src = len(pdoc), len(src_reader.pages)
        add("reopen", True, "Output opens in two independent PDF parsers.")
    except Exception as exc:  # noqa: BLE001
        add("reopen", False, f"Output could not be reopened ({type(exc).__name__}).")
        return {"checks": checks, "outcome": "FAILED"}
    add("page_count", n_out == n_src == len(reader.pages), f"{n_src} pages in, {n_out} out.")
    # page geometry
    geom = all(abs(float(a.mediabox.width) - float(b.mediabox.width)) < .01 and abs(float(a.mediabox.height) - float(b.mediabox.height)) < .01
               for a, b in zip(src_reader.pages, reader.pages))
    add("page_geometry", geom, "Page sizes are unchanged.")
    # structure: annotations / images / form fields / fonts must not change
    def struct(rd, data):
        d = pdfium.PdfDocument(data)
        images = sum(1 for i in range(len(d)) for o in d[i].get_objects() if o.type == 3)
        annots = sum(len(p.get("/Annots") or []) for p in rd.pages)
        fields = len(rd.get_fields() or {})
        return images, annots, fields
    s_in, s_out = struct(src_reader, source), struct(reader, out)
    add("images_preserved", s_in[0] == s_out[0], f"Images {s_in[0]} → {s_out[0]}.")
    add("annotations_preserved", s_in[1] == s_out[1], f"Annotations {s_in[1]} → {s_out[1]}.")
    add("form_fields_preserved", s_in[2] == s_out[2], f"Form fields {s_in[2]} → {s_out[2]}.")
    # 2. edited text really present, old really gone
    sdoc = pdfium.PdfDocument(source)
    pages = sorted({p["page"] for p in plans})
    new_engine = PdfTextEngine(out)
    for p in plans:
        text_out = _norm(pdoc[p["page"]].get_textpage().get_text_range())
        text_in = _norm(sdoc[p["page"]].get_textpage().get_text_range())
        present = _norm(p["new"]) in text_out
        old_n = _norm(p["old"])
        gone = (not any(old_n in line for line in _lines(pdoc[p["page"]].get_textpage().get_text_range()))
                or old_n in _norm(p["new"]) or sum(line.count(old_n) for line in _lines(sdoc[p["page"]].get_textpage().get_text_range())) > 1)
        add("new_text_present", present, f"Edit on page {p['page'] + 1}: new text is extractable from the saved PDF.")
        add("old_text_removed", gone, f"Edit on page {p['page'] + 1}: original wording is no longer in the page content.")
    # 3. untouched runs identical
    for page in pages:
        edited = {pl["id"] for pl in plans if pl["page"] == page}
        keep = {(round(r.x0, 1), round(r.y, 1), r.text) for r in before_runs[page] if r.id not in edited
                and not any(abs(r.y - pl["y"]) < 1 and r.x0 < pl["x0"] + pl["new_width"] + 1 and r.x1 > pl["x0"] - 1 for pl in plans if pl["page"] == page)}
        now = {(round(r.x0, 1), round(r.y, 1), r.text) for r in new_engine.runs(page)}
        lost = keep - now
        add("unedited_text_preserved", not lost, f"Page {page + 1}: {len(keep) - len(lost)} of {len(keep)} other text runs are identical.")
    # 3b. every new visible character really drew something (catches glyphs missing from an embedded font program)
    for p in plans:
        page = p["page"]
        scale = 3.0
        img = _render(out, page, scale)
        H = float(src_reader.pages[page].mediabox.height)
        x_start, em = p["x0"], p["size"]
        total = max(p["new_width"], 1e-6)
        y_top, y_bot = int((H - (p["y"] + 0.82 * em)) * scale), int((H - (p["y"] - 0.22 * em)) * scale)
        band = img[max(y_top, 0):max(y_bot, 1), :]
        blank = []
        for ch, (o0, o1) in zip(p["new"], p["cells"]):
            if ch.isspace():
                continue
            c0, c1 = int((x_start + o0) * scale), int((x_start + o1) * scale) + 1
            cell = band[:, max(c0, 0):max(c1, c0 + 1)]
            if cell.size == 0:
                continue
            bg = np.bincount(np.clip(cell.ravel(), 0, 255).astype(np.int64)).argmax()
            if not (np.abs(cell - bg) > 40).any():
                blank.append(ch)
        add("new_glyphs_rendered", len(blank) <= 0, f"Edit on page {page + 1}: every new character is visible on the page." if not blank
            else f"Edit on page {page + 1}: {len(blank)} new character(s) did not draw.")
    # 4. pixels: change confined to edited boxes; untouched pages identical
    for page in range(n_src):
        if page in pages:
            a, b = _render(source, page, 2.0), _render(out, page, 2.0)
            diff = np.abs(a - b) > PIXEL_THRESHOLD
            H = float(src_reader.pages[page].mediabox.height)
            allowed = np.zeros_like(diff)
            for pl in plans:
                if pl["page"] != page:
                    continue
                x0, y0, x1, y1 = pl["bbox"]
                x1 = max(x1, pl["x0"] + pl["new_width"])
                allowed[max(int((H - y1 - PAD_PT) * 2), 0):int((H - y0 + PAD_PT) * 2) + 1,
                        max(int((x0 - PAD_PT) * 2), 0):int((x1 + PAD_PT) * 2) + 1] = True
            stray = int((diff & ~allowed).sum())
            add("changes_confined", stray == 0, f"Page {page + 1}: {int(diff.sum())} pixels changed, {stray} outside the edited text.")
        else:
            same = bool((_render(source, page, 0.6) == _render(out, page, 0.6)).all())
            if not same:
                add("untouched_pages_identical", False, f"Page {page + 1} changed although it was not edited.")
    if not any(c["check"] == "untouched_pages_identical" for c in checks):
        add("untouched_pages_identical", True, f"All {n_src - len(pages)} unedited pages render identically.")
    ok = all(c["status"] == "pass" for c in checks if c["critical"])
    return {"checks": checks, "outcome": "VERIFIED" if ok else "FAILED"}


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--job", required=True)
    job = json.load(open(ap.parse_args().job))
    source = open(job["source"], "rb").read()
    try:
        if job["mode"] == "runs":
            result = list_runs(source, int(job["page"]))
        elif job["mode"] == "outline":
            result = outline(source)
        else:
            applied = apply_edits(source, job["edits"])
            if "error" in applied:
                result = applied
            else:
                report = verify(source, applied["out"], applied["plans"], applied["before_runs"])
                with open(job["out"], "wb") as handle:
                    handle.write(applied["out"])
                result = {**report, "source_sha256": hashlib.sha256(source).hexdigest(),
                          "output_sha256": hashlib.sha256(applied["out"]).hexdigest(), "output_bytes": len(applied["out"]),
                          "edits": [{k: pl[k] for k in ("id", "page", "font", "size", "substitute_font")} for pl in applied["plans"]]}
    except EditRefused as err:
        result = {"error": err.code, "message": err.message}
    except Exception as exc:  # noqa: BLE001 - fail closed with a stable code, no document data in the message
        result = {"error": "pdf_edit_worker_failed", "stage": type(exc).__name__}
    sys.stdout.write(json.dumps(result, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
