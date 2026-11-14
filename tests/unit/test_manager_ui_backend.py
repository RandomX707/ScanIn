from __future__ import annotations

from pathlib import Path

import pytest

from packages.api.errors import APIError
from packages.api.save_flow import FORMULA_NOTE, SaveRejected, _coerce, _validate_text, encode_like, plain
from packages.api.workspace import DEMO_FILENAME, SAMPLE_FILES, SAMPLES_DIR, download_name, version_name
from packages.core import extraction
from packages.core.chunking import chunkify


def _demo_text() -> str:
    return (SAMPLES_DIR / DEMO_FILENAME).read_text(encoding="utf-8")


def test_chunking_matches_prototype_sections_and_takeaways():
    sections = chunkify(_demo_text())
    assert [s["title"] for s in sections][:3] == [
        "Meridian Health · Group Coverage Agreement", "01. Coverage overview", "02. Eligibility and enrollment"]
    assert len(sections) == 8
    assert sections[0]["points"] == [
        "Policy reference: MH-GC-2027-1048", "Effective period: January 01, 2027 – December 31, 2027",
        "Prepared for: Northstar Services (demonstration only)"]
    assert sections[1]["points"][2] == "The annual aggregate benefit ceiling is INR 500,000 per enrolled family."
    assert sections[1]["words"] == 62
    assert chunkify(_demo_text()) == sections  # deterministic


def test_chunking_empty_and_dataset_sections():
    assert chunkify("") == [] and chunkify("  \n ") == []
    table = chunkify("# Table: a.csv\nMonth | Sub | App\nJan | 3 | 4")[0]
    assert table["summary"] == "Structured dataset with 1 records, covering Month, Sub, App."
    assert table["points"][0] == "2 rows including headings"


def test_every_sample_is_present_and_text_formats_extract():
    for name in SAMPLE_FILES.values():
        assert (SAMPLES_DIR / name).is_file()
    assert extraction.html_to_text("<h2>Hi</h2><p>A <b>b</b></p><script>x()</script>") == "## Hi\nA b"
    assert extraction.csv_text("a.csv", "x,y\n1,2\n") == "# Table: a.csv\nx | y\n1 | 2"
    assert extraction.json_text('{"a":1}') == '{\n  "a": 1\n}'


def test_version_names_keep_the_original_extension():
    assert version_name("Report.docx", 1) == "Report.docx"
    assert version_name("Report.docx", 2) == "Report · v2.docx"
    assert download_name("Report.docx", 3) == "Report_v3.docx"


@pytest.mark.parametrize("original,text,expected", [
    (b"a\nb\n", "a\nB\n", b"a\nB\n"),
    (b"a\r\nb\r\n", "a\nB\n", b"a\r\nB\r\n"),
    (b"\xef\xbb\xbfhi", "hello", b"\xef\xbb\xbfhello"),
    (b"caf\xe9", "cafés", b"caf\xe9s"),
])
def test_text_writer_keeps_encoding_and_line_endings(original, text, expected):
    assert encode_like(original, text)[0] == expected


def test_text_writer_rejects_unrepresentable_characters():
    with pytest.raises(SaveRejected) as error:
        encode_like(b"caf\xe9", "snowman ☃")
    assert error.value.message == "This text has characters the file's encoding can't hold."


def test_text_validation_messages():
    _validate_text(".md", "# ok")
    for suffix, text, message in [
        (".json", '{"a": ', "This isn't valid JSON yet. Fix it and save again."),
        (".xml", "<a><b></a>", "This isn't well-formed XML yet. Fix it and save again."),
        (".txt", "   ", "The edited document is empty. Add some content before saving."),
    ]:
        with pytest.raises(APIError) as error:
            _validate_text(suffix, text)
        assert error.value.message == message


def test_value_coercion_follows_the_cell_type():
    assert _coerce("integer", "42") == {"type": "integer", "value": 42}
    assert _coerce("decimal", "12.50") == {"type": "decimal", "value": "12.50"}
    assert _coerce("boolean", "Yes") == {"type": "boolean", "value": True}
    with pytest.raises(SaveRejected):
        _coerce("integer", "4.2")
    with pytest.raises(SaveRejected):
        _coerce("decimal", "NaN")


def test_plain_sentences_for_reason_codes():
    assert plain("max_length_exceeded") == "This value doesn't fit the field."
    assert plain("formula_cell").startswith("Formula cell")
    assert FORMULA_NOTE == "Formulas update when opened in Excel."


def test_samples_directory_is_packaged():
    assert Path(SAMPLES_DIR).is_dir()


def test_text_versions_report_saved_not_verified():
    from datetime import datetime, timezone
    from packages.api.workspace import _folder_payload
    now = datetime.now(timezone.utc)
    v = lambda i, kind, parent, outcome=None: {"version_id": f"v{i}", "kind": kind, "created_at": now, "size_bytes": 1,
                                               "parent_version_id": parent, "verification_outcome": outcome}
    folder = {"id": "d", "filename": "a.md", "created_at": now,
              "versions": [v(1, "original", None), v(2, "derived", "v1"), v(3, "derived", "v1", "VERIFIED")]}
    payload = _folder_payload(folder)["versions"]
    assert [x["outcome"] for x in payload] == [None, "SAVED", "VERIFIED"]
    assert payload[2]["parent_number"] == 1 and payload[2]["number"] == 3  # edits from v1 derive from v1
