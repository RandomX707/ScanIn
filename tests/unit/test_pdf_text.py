"""Real content-stream text editing on synthetic PDFs (no stakeholder data)."""
import io
import numpy as np
import pypdfium2 as pdfium
import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
import reportlab, os

from packages.core.pdf_text import EditRefused, PdfTextEngine


@pytest.fixture(scope="module")
def sample() -> bytes:
    vera = os.path.join(os.path.dirname(reportlab.__file__), "fonts", "Vera.ttf")
    pdfmetrics.registerFont(TTFont("Vera", vera))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Vera", 14); c.drawString(72, 760, "Policy Schedule Heading")
    c.setFont("Vera", 10)
    c.drawString(72, 730, "Sum insured 5000000 rupees")
    c.drawString(72, 712, "Premium payable 1200 only")
    c.drawString(400, 730, "RIGHT BLOCK")          # neighbour that limits growth of the line on its left
    c.showPage(); c.setFont("Vera", 10); c.drawString(72, 700, "Second page text unchanged"); c.save()
    return buf.getvalue()


def _render(b, i):
    return np.asarray(pdfium.PdfDocument(b)[i].render(scale=2).to_pil().convert("L"), dtype=np.int16)


def _text(b, i):
    return pdfium.PdfDocument(b)[i].get_textpage().get_text_range().replace("\r\n", " ")


def _run(e, startswith):
    return next(r for r in e.runs(0) if r.text.startswith(startswith))


def test_edit_replaces_real_text_not_overlay(sample):
    e = PdfTextEngine(sample)
    run = _run(e, "Premium payable")
    e.replace(run.id, "Premium due 1200", expect_old=run.text)
    out = e.save()
    assert len(PdfReader(io.BytesIO(out)).pages) == 2
    page1 = _text(out, 0)
    assert "Premium due 1200" in page1 and "Premium payable" not in page1       # old glyphs are really gone
    assert "Sum insured 5000000 rupees" in page1 and "Policy Schedule Heading" in page1
    assert (_render(sample, 1) == _render(out, 1)).all()                         # other page pixel-identical
    diff = np.abs(_render(sample, 0) - _render(out, 0)) > 8
    ys, xs = np.where(diff)
    x0, y0, x1, y1 = run.bbox
    H = 841.89
    assert xs.min() / 2 >= x0 - 2 and ys.min() / 2 >= H - y1 - 2 and ys.max() / 2 <= H - y0 + 2   # change confined to the run's line


def test_untouched_runs_keep_position(sample):
    e = PdfTextEngine(sample)
    before = {(round(r.x0, 1), round(r.y, 1), r.text) for r in e.runs(0)}
    run = _run(e, "Sum insured")
    e.replace(run.id, "Sum insured 900000 rupees")
    e2 = PdfTextEngine(e.save())
    after = {(round(r.x0, 1), round(r.y, 1), r.text) for r in e2.runs(0)}
    assert {t for t in before if not t[2].startswith("Sum insured")} <= after


def test_missing_glyph_refused_without_substitution(sample):
    e = PdfTextEngine(sample)
    run = _run(e, "Premium payable")
    with pytest.raises(EditRefused) as err:
        e.replace(run.id, "Premium payable ₹")
    assert err.value.code == "glyph_unavailable"


def test_overflow_refused(sample):
    e = PdfTextEngine(sample)
    run = _run(e, "Sum insured")
    with pytest.raises(EditRefused) as err:
        e.replace(run.id, "Sum insured 5000000 rupees " + "Premium payable " * 4)
    assert err.value.code == "overflow"


def test_stale_and_noop_refused(sample):
    e = PdfTextEngine(sample)
    run = _run(e, "Premium payable")
    with pytest.raises(EditRefused) as err:
        e.replace(run.id, "Premium due", expect_old="something else")
    assert err.value.code == "stale_target"
    with pytest.raises(EditRefused) as err:
        e.replace(run.id, run.text)
    assert err.value.code == "no_change"
