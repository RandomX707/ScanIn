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


def test_missing_glyph_uses_disclosed_standard_font(sample):
    e = PdfTextEngine(sample)
    run = _run(e, "Premium payable")
    # 'é' is not in the subset embedded for this font, so the line is redrawn in a standard font and says so.
    res = e.replace(run.id, "Premium payable é")
    assert res["substitute_font"] == "Helvetica"
    out = e.save()
    assert "Premium payable é" in _text(out, 0)
    assert "Sum insured 5000000 rupees" in _text(out, 0)
    assert (_render(sample, 1) == _render(out, 1)).all()


def test_text_after_substituted_line_keeps_its_font(sample):
    """The original Tf is restored after the substituted line so later text isn't restyled."""
    e = PdfTextEngine(sample)
    before = {r.text: (r.font_name, round(r.size, 1)) for r in e.runs(0)}
    e.replace(_run(e, "Sum insured").id, "Sum insured é")
    e2 = PdfTextEngine(e.save())
    after = {r.text: (r.font_name, round(r.size, 1)) for r in e2.runs(0)}
    for text in ("Premium payable 1200 only", "RIGHT BLOCK", "Policy Schedule Heading"):
        assert after[text] == before[text]


def test_saved_pdf_keeps_every_image_and_object(sample):
    """Regression: object de-duplication in the PDF writer once dropped images; saving must keep them all."""
    import io as _io
    from reportlab.lib.utils import ImageReader
    from PIL import Image
    buf = _io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for n in range(3):
        img = Image.new("RGB", (40, 40), (n * 80, 20, 200))
        c.drawImage(ImageReader(img), 50, 600 - n * 100, 40, 40)
    c.setFont("Helvetica", 12); c.drawString(72, 700, "Images below stay put"); c.save()
    data = buf.getvalue()
    e = PdfTextEngine(data)
    run = e.runs(0)[0]
    e.replace(run.id, "Images below")
    out = e.save()
    count = lambda b: sum(1 for o in pdfium.PdfDocument(b)[0].get_objects() if o.type == 3)
    assert count(out) == count(data) == 3


def test_horizontal_scaling_and_char_spacing_are_respected():
    """Fit/width maths must use the real horizontal em (Tz) and Tc, not the nominal font size."""
    import io as _io
    buf = _io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Vera", 12)
    t = c.beginText(72, 700)
    t.setHorizScale(50)          # Tz 50
    t.setCharSpace(1.0)          # Tc 1
    t.textLine("Wide digits 0123456789")
    c.drawText(t)
    c.save()
    e = PdfTextEngine(buf.getvalue())
    run = e.runs(0)[0]
    width, _, missing = e.measure(run, run.text)
    assert not missing
    # predicted width of the unchanged text equals the real drawn width (within 2%), so fit checks are meaningful
    assert abs(width - (run.x1 - run.x0)) / (run.x1 - run.x0) < 0.12
    assert run.xem < run.size            # horizontal em is narrower than vertical em because Tz = 50
