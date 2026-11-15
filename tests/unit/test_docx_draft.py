"""Draft candidates for the DOCX page editor: native text replacement only, verified against fresh targets, original never touched."""
from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from docx import Document

from packages.core.adapters.docx.adapter import DocxAdapter, DocxDocumentError


def _doc() -> bytes:
    d = Document()
    d.add_heading("Quotation", 1)
    d.add_paragraph("Premium is payable annually.")
    t = d.add_table(rows=2, cols=2)
    for r, row in enumerate([("Item", "Amount"), ("Premium", "1000")]):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    mixed = d.add_paragraph()
    mixed.add_run("Mixed ").bold = True
    mixed.add_run("styles here")
    buf = io.BytesIO(); d.save(buf); return buf.getvalue()


def _target(src: bytes, text: str):
    return next(t for t in DocxAdapter().targets(src) if t.editable and t.value.value == text)


def test_draft_changes_only_the_selected_target_and_never_the_source():
    src = _doc(); sha = hashlib.sha256(src).hexdigest()
    t = _target(src, "1000")
    out = DocxAdapter().draft(src, [(t.id, t.fingerprint, "25,00,00,000,678")])
    assert hashlib.sha256(src).hexdigest() == sha
    assert Document(io.BytesIO(out)).tables[0].cell(1, 1).text == "25,00,00,000,678"
    before = {x.id: x.value.value for x in DocxAdapter().targets(src) if not x.id.endswith(":multi_run")}
    after = {x.id: x.value.value for x in DocxAdapter().targets(out) if not x.id.endswith(":multi_run")}
    assert [k for k in before if before[k] != after[k]] == [t.id]
    assert len(Document(io.BytesIO(out)).tables[0].rows) == 2          # table structure intact


def test_identical_texts_are_distinguished_by_target_id():
    d = Document(); tb = d.add_table(rows=1, cols=2); tb.cell(0, 0).text = "Yes"; tb.cell(0, 1).text = "Yes"
    buf = io.BytesIO(); d.save(buf); src = buf.getvalue()
    yes = [t for t in DocxAdapter().targets(src) if t.editable and t.value.value == "Yes"]
    assert len(yes) == 2 and yes[0].id != yes[1].id
    out = DocxAdapter().draft(src, [(yes[1].id, yes[1].fingerprint, "No")])
    row = Document(io.BytesIO(out)).tables[0].rows[0]
    assert (row.cells[0].text, row.cells[1].text) == ("Yes", "No")


def test_stale_fingerprint_unknown_target_and_duplicates_are_refused():
    src = _doc(); t = _target(src, "1000")
    for edits, code in [([(t.id, "0" * 64, "x")], "fingerprint_mismatch"), ([("docx:word/document.xml:p99:r0:t0", t.fingerprint, "x")], "target_not_found"),
                        ([(t.id, t.fingerprint, "a"), (t.id, t.fingerprint, "b")], "duplicate_target"), ([], "no_change_has_no_candidate"),
                        ([(t.id, t.fingerprint, "bad\x00text")], "docx_text_requires_string")]:
        with pytest.raises(DocxDocumentError) as error:
            DocxAdapter().draft(src, edits)
        assert error.value.code == code


def test_read_only_multi_run_paragraph_cannot_be_drafted():
    src = _doc()
    span = next(t for t in DocxAdapter().targets(src) if t.id.endswith(":multi_run"))
    assert not span.editable
    with pytest.raises(DocxDocumentError) as error:
        DocxAdapter().draft(src, [(span.id, span.fingerprint, "x")])
    assert error.value.code == "multi_run_span"


def test_candidate_keeps_every_other_package_part_byte_identical():
    src = _doc(); t = _target(src, "Quotation")
    out = DocxAdapter().draft(src, [(t.id, t.fingerprint, "Quote")])
    with zipfile.ZipFile(io.BytesIO(src)) as a, zipfile.ZipFile(io.BytesIO(out)) as b:
        assert a.namelist() == b.namelist()
        changed = [n for n in a.namelist() if a.read(n) != b.read(n)]
    assert changed == ["word/document.xml"]
