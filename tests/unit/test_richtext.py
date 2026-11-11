from __future__ import annotations

import pytest

from packages.api.workspace import DEMO_FILENAME, SAMPLES_DIR
from packages.core import richtext as r


def _demo() -> str:
    return (SAMPLES_DIR / DEMO_FILENAME).read_text(encoding="utf-8")


def test_untouched_markdown_round_trips_with_unchanged_rendered_text():
    source = _demo()
    html = r.render_markdown(source)
    new = r.serialise("md", source, html)
    r.check_untouched_unchanged("md", source, new, html)
    assert r.block_texts(r.render_markdown(new)) == r.block_texts(html)
    assert r.html_to_markdown("<h2>Title</h2><ul><li>a</li><li>b</li></ul><p><strong>x</strong> <em>y</em></p>") == \
        "## Title\n\n- a\n- b\n\n**x** *y*\n"


def test_edit_changes_only_the_edited_block():
    source = _demo()
    html = r.render_markdown(source).replace("INR 8,000", "INR 9,000")
    new = r.serialise("md", source, html)
    r.check_untouched_unchanged("md", source, new, html)
    assert "INR 9,000" in new and "INR 8,000" not in new
    before, after = r.block_texts(r.render_markdown(source)), r.block_texts(r.render_markdown(new))
    assert sum(a != b for a, b in zip(before, after)) == 1 and len(before) == len(after)


def test_save_is_refused_when_untouched_text_would_change():
    source = "# T\n\nOne paragraph.\n\nSecond paragraph.\n"
    html = r.render_markdown(source)
    tampered = "# T\n\nOne paragraph.\n\nSecond paragraph changed.\n"  # serialiser output differing from the edited HTML
    with pytest.raises(r.RichTextRejected):
        r.check_untouched_unchanged("md", source, tampered, html)


def test_html_is_sanitised_and_head_is_kept():
    source = "<!doctype html><html><head><title>Keep</title></head><body><p>Hi</p></body></html>"
    edited = '<p>Hi there</p><script>alert(1)</script><p onclick="x()">Safe <a href="javascript:bad()">link</a></p>'
    new = r.serialise("html", source, edited)
    assert "<title>Keep</title>" in new and "<script" not in new and "onclick" not in new and "javascript:" not in new
    r.check_untouched_unchanged("html", source, new, edited)


def test_markdown_underline_is_kept_as_inline_html():
    assert "<u>x</u>" in r.html_to_markdown("<p><u>x</u></p>")
