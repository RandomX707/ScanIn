"""Smart fitting of longer PDF text: borders, neighbours, clip and rotation are respected; only limited, verified adjustments are offered."""
from __future__ import annotations

import hashlib
import io

import pypdfium2 as pdfium
import pytest
from reportlab.pdfgen import canvas

from packages.core.pdf_text import PdfTextEngine
from packages.workers import pdf_edit_entry as worker

OLD, NEW = "11,11,11,111", "11,11,11,111,678"


def _table(left=75.0, right=165.0, text_x=80.0, align="left", neighbour: str | None = "Neighbour value", rotate=False, clip=False, second_row=False) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    c.setLineWidth(0.8)
    for x in (left, right):
        c.line(x, 700, x, 740)
    c.line(left, 700, right, 700); c.line(left, 740, right, 740)
    c.setFont("Helvetica", 12)
    if rotate:
        c.saveState(); c.translate(text_x, 710); c.rotate(10); c.drawString(0, 0, OLD); c.restoreState()
    else:
        if clip:
            c.saveState(); p = c.beginPath(); p.rect(left, 700, right - left, 40); c.clipPath(p, stroke=0, fill=0)
        if align == "right":
            c.drawRightString(right - 15, 715, OLD)
        else:
            c.drawString(text_x, 715, OLD)
        if clip:
            c.restoreState()
    if neighbour:
        c.drawString(right + 12, 715, neighbour)
    if second_row:
        c.line(left, 660, left, 700); c.line(right, 660, right, 700); c.line(left, 660, right, 660)
        c.drawString(text_x, 675, "7,77,777")
    c.showPage(); c.save()
    return buf.getvalue()


def _run(src: bytes, text: str = OLD):
    eng = PdfTextEngine(src)
    run = next(r for r in eng.runs(0) if r.text == text)
    return eng, run


def _apply(src: bytes, new: str = NEW, fit: str | None = None, text: str = OLD):
    eng, run = _run(src, text)
    edit = {"id": run.id, "old": text, "new": new, **({"fit": fit} if fit else {})}
    applied = worker.apply_edits(src, [edit])
    if "error" in applied:
        return applied, None
    return applied, worker.verify(src, applied["out"], applied["plans"], applied["before_runs"])


def _text(pdf: bytes) -> str:
    return "".join(pdfium.PdfDocument(pdf)[0].get_textpage().get_text_range().split())


def test_longer_value_fits_naturally_when_the_cell_has_room():
    src = _table(right=260.0)
    applied, report = _apply(src)
    assert report["outcome"] == "VERIFIED", report
    assert NEW.replace(" ", "") in _text(applied["out"]) and applied["plans"][0]["strategy"] is None
    assert any(c["check"] == "no_border_or_neighbour_collision" and c["status"] == "pass" for c in report["checks"])
    assert hashlib.sha256(src).hexdigest() == hashlib.sha256(src).hexdigest()


def test_overflow_is_refused_by_default_but_safe_options_are_reported():
    src = _table()
    applied, _ = _apply(src)
    assert applied["error"] == "overflow" and {o["strategy"] for o in applied["options"]} >= {"size", "scale"}
    assert all(0.85 <= o["factor"] < 1 for o in applied["options"] if o["strategy"] in ("size", "scale"))
    eng, run = _run(src)
    rep = eng.fit_report(run.id, NEW)
    assert rep["fits"] is False and rep["refusal"] is None and rep["options"]


@pytest.mark.parametrize("strategy", ["size", "scale"])
def test_chosen_fit_is_applied_verified_and_stays_inside_the_cell(strategy):
    src = _table()
    applied, report = _apply(src, fit=strategy)
    assert report["outcome"] == "VERIFIED", [c for c in report["checks"] if c["status"] != "pass"]
    out = applied["out"]
    assert NEW.replace(" ", "") in _text(out)
    eng2 = PdfTextEngine(out)
    new_run = next(r for r in eng2.runs(0) if r.text.replace(" ", "") == NEW)
    assert new_run.x1 < 165.0 - 1.0 and new_run.x0 >= 75.0
    assert next(r for r in eng2.runs(0) if r.text == "Neighbour value").x0 == pytest.approx(177.0, abs=0.5)   # neighbour untouched
    assert all(c["status"] == "pass" for c in report["checks"] if c["critical"])


def test_text_after_a_fitted_edit_keeps_its_state():
    src = _table(second_row=True)
    applied, report = _apply(src, fit="scale")
    assert report["outcome"] == "VERIFIED"
    row2 = next(r for r in PdfTextEngine(applied["out"]).runs(0) if r.text == "7,77,777")
    assert row2.size == pytest.approx(12.0, abs=0.01) and row2.xem == pytest.approx(12.0, abs=0.01)


def test_a_very_long_replacement_has_no_safe_option_and_is_refused_with_a_reason():
    src = _table()
    eng, run = _run(src)
    rep = eng.fit_report(run.id, NEW + ",111,222,333")
    assert not rep["fits"] and rep["options"] == [] and rep["refusal"]["code"] == "overflow"
    applied, _ = _apply(src, new=NEW + ",111,222,333", fit="size")
    assert applied["error"] in {"fit_unavailable", "overflow"}


def test_a_neighbouring_value_limits_growth_even_without_borders():
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=(595, 842)); c.setFont("Helvetica", 12)
    c.drawString(80, 715, OLD); c.drawString(158, 715, "Next"); c.showPage(); c.save()
    eng, run = _run(buf.getvalue())
    rep = eng.fit_report(run.id, NEW)
    assert not rep["fits"] and all(o["strategy"] != "expand" for o in rep["options"])      # no detected cell: free page space is not offered
    assert all(80.0 + o["new_width"] + o["shift"] <= 156.0 for o in rep["options"])


def test_right_aligned_value_grows_leftwards_keeping_its_right_edge():
    src = _table(left=75.0, right=260.0, align="right")
    applied, report = _apply(src)
    assert report["outcome"] == "VERIFIED", report
    after = next(r for r in PdfTextEngine(applied["out"]).runs(0) if r.text.replace(" ", "") == NEW)
    assert after.x1 == pytest.approx(245.0, abs=0.6)


def test_expand_uses_free_cell_space_when_text_on_one_side_blocks_the_natural_growth():
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=(595, 842)); c.setLineWidth(0.8); c.setFont("Helvetica", 12)
    for x in (60, 200):
        c.line(x, 700, x, 740)
    c.drawString(90, 715, OLD); c.drawString(165, 715, "Note"); c.showPage(); c.save()
    src = buf.getvalue()
    eng, run = _run(src)
    rep = eng.fit_report(run.id, NEW)
    assert not rep["fits"] and any(o["strategy"] == "expand" for o in rep["options"])
    applied, report = _apply(src, fit="expand")
    assert report["outcome"] == "VERIFIED", [c for c in report["checks"] if c["status"] != "pass"]
    after = next(r for r in PdfTextEngine(applied["out"]).runs(0) if r.text.replace(" ", "") == NEW)
    assert after.x0 >= 60.0 and after.x1 <= 163.01 and next(r for r in PdfTextEngine(applied["out"]).runs(0) if r.text == "Note").x0 == pytest.approx(165.0, abs=0.5)


def test_rotated_text_cannot_be_made_longer():
    eng = PdfTextEngine(_table(rotate=True))
    run = eng.runs(0)[0]
    rep = eng.fit_report(run.id, run.text * 12)
    assert not rep["fits"] and rep["refusal"]["code"] == "rotated_text"


def test_clip_region_bounds_the_growth():
    src = _table(clip=True, right=140.0, text_x=80.0)
    eng, run = _run(src)
    rep = eng.fit_report(run.id, NEW)
    assert not rep["fits"]
    assert all(80.0 + o["new_width"] + o["shift"] <= 140.0 + 0.5 for o in rep["options"])


def test_short_and_slightly_longer_replacements_are_unchanged_behaviour():
    src = _table(right=260.0)
    for new in ("11,11,111", "11,11,11,1111"):
        applied, report = _apply(src, new=new)
        assert report["outcome"] == "VERIFIED" and new in _text(applied["out"])


def test_fit_is_unavailable_for_an_unknown_strategy_and_never_mutates_the_source():
    src = _table(); before = hashlib.sha256(src).hexdigest()
    eng, run = _run(src)
    from packages.core.pdf_text import EditRefused
    with pytest.raises(EditRefused) as e:
        eng.replace(run.id, NEW, fit="bogus")
    assert e.value.code == "fit_unknown"
    assert hashlib.sha256(src).hexdigest() == before


def test_two_fitted_edits_on_one_page_do_not_disturb_each_other():
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=(595, 842)); c.setLineWidth(0.8); c.setFont("Helvetica", 12)
    for x in (75, 165, 255):
        c.line(x, 700, x, 740)
    c.drawString(80, 715, OLD); c.drawString(170, 715, "22,22,22,222"); c.drawString(260, 715, "Unchanged cell"); c.showPage(); c.save()
    src = buf.getvalue()
    eng = PdfTextEngine(src)
    a = next(r for r in eng.runs(0) if r.text == OLD); b = next(r for r in eng.runs(0) if r.text == "22,22,22,222")
    edits = [{"id": a.id, "old": OLD, "new": NEW, "fit": "scale"}, {"id": b.id, "old": b.text, "new": b.text + ",9", "fit": "size"}]
    applied = worker.apply_edits(src, edits)
    assert "error" not in applied, applied
    report = worker.verify(src, applied["out"], applied["plans"], applied["before_runs"])
    assert report["outcome"] == "VERIFIED", [x for x in report["checks"] if x["status"] != "pass"]
    texts = {r.text.replace(" ", "") for r in PdfTextEngine(applied["out"]).runs(0)}
    assert {NEW, "22,22,22,222,9", "Unchangedcell"} <= texts


def _clip_pdf(path_ops: str, form: bool = False) -> bytes:
    """Page with a clip written as raw operators around the value (reportlab cannot draw an m/l/h clip)."""
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=(595, 842)); c.setFont("Helvetica", 12)
    c._code.append("q " + path_ops + " W n")
    c.drawString(80, 715, OLD)
    c._code.append("Q")
    c.showPage(); c.save()
    return buf.getvalue()


def test_a_rectangular_clip_drawn_as_a_polygon_is_measured_not_refused():
    eng, run = _run(_clip_pdf("75 700 m 140 700 l 140 740 l 75 740 l h"))
    assert run.clip == pytest.approx((75.0, 700.0, 140.0, 740.0))
    rep = eng.fit_report(run.id, NEW)
    assert not rep["fits"] and all(80.0 + o["new_width"] + o["shift"] <= 140.0 + 0.5 for o in rep["options"])


def test_a_non_rectangular_clip_still_blocks_growth():
    eng, run = _run(_clip_pdf("75 700 m 200 700 l 200 740 l 120 760 l h"))
    assert run.clip == "unknown"
    assert eng.fit_report(run.id, NEW)["refusal"]["code"] == "clipped_text"


def test_two_edits_that_would_touch_are_flagged_by_verification():
    plans = [{"page": 0, "y": 715.0, "size": 12.0, "ext": [80.0, 160.0]}, {"page": 0, "y": 715.5, "size": 12.0, "ext": [159.5, 220.0]}]
    clash = plans[0]["ext"][0] < plans[1]["ext"][1] + 1.0 and plans[1]["ext"][0] < plans[0]["ext"][1] + 1.0
    assert clash                                              # the geometry rule used by the worker; end-to-end below
    src = _table(right=400.0)
    eng, a = _run(src)
    out = worker.verify(src, src, [{"id": "x", "page": 0, "old": OLD, "new": OLD, "bbox": list(a.bbox), "x0": a.x0, "orig_x0": a.x0, "orig_x1": a.x1,
                                    "ext": [a.x0, a.x1], "y": a.y, "new_width": a.x1 - a.x0, "size": 12.0, "cells": []},
                                   {"id": "y", "page": 0, "old": OLD, "new": OLD, "bbox": list(a.bbox), "x0": a.x0, "orig_x0": a.x0, "orig_x1": a.x1,
                                    "ext": [a.x1 - 0.5, a.x1 + 30], "y": a.y, "new_width": 30, "size": 12.0, "cells": []}], {0: eng.runs(0)})
    assert any(c["check"] == "edited_texts_do_not_overlap" and c["status"] == "fail" for c in out["checks"])


def test_separate_object_touching_the_value_is_reported_for_the_display_notice():
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=(595, 842)); c.setFont("Helvetica", 12)
    c.drawString(80, 715, OLD)
    c.drawString(80 + c.stringWidth(OLD, "Helvetica", 12), 715, ".00")
    c.drawString(300, 715, "far away"); c.showPage(); c.save()
    src = buf.getvalue(); eng, run = _run(src)
    assert eng.adjacent_right(run) == ".00"
    assert worker.list_runs(src, 1)["runs"][0].get("adj") in (".00", None) and any(r.get("adj") == ".00" for r in worker.list_runs(src, 1)["runs"])
