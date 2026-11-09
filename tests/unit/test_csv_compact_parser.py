"""Phase 2B differential tests: the compact offset-indexed CSV parser must match the frozen f6139a6 parser
cell-for-cell (coordinates, byte spans, raw bytes, native text, values, quoting), in row structure,
line-ending detection and error codes, on fixtures and on randomized inputs."""
from __future__ import annotations

import hashlib
import random
from pathlib import Path

import pytest

from packages.core.adapters.csv_adapter.adapter import (MAX_CSV_CELLS, MAX_CSV_ROWS, CsvAdapter, CsvError)
from tests.unit._legacy_csv import CsvError as LegacyError, LegacyCsvParser

PACKS = [Path("artifacts/e2e-pack-run/pack/channel360-test-pack/csv"), Path("artifacts/phase2a-run/pack40/csv")]
FIXTURES = sorted(p for pack in PACKS if pack.exists() for p in pack.glob("*.csv") if p.stat().st_size < 2_000_000)

HANDWRITTEN = [
    b"a,b,c\n1,2,3\n",
    b"a,b,\nc\n",                       # trailing delimiter before a newline drops the empty field (legacy rule)
    b"a,b,",                             # ...but at end of file it is kept
    b"\n\na,b\n\n\nc\n\n",               # blank lines are rows without cells
    b'"x,y","he said ""hi""",z\r\n"multi\r\nline",2,3\r\n',
    b'h1,h1,,h4\n1,,3\n4,5,6,7,8\n',     # duplicate and empty headers, ragged rows
    b"\xef\xbb\xbfclaim,note\r\nCLM-1,caf\xc3\xa9\r\n",
    b"claim,note\nCLM-1,caf\xe9\n",      # cp1252
    b"a;b;c\n1;2,5;3\n",                 # semicolon dialect
    b"a\tb\n1\t2\n",
    b"a,b\r\nc,d\ne,f\r\n",              # mixed endings
    b"a,b\rc,d\r",                       # CR only
    b'a,"b\nc\rd\r\ne"\n',               # embedded newlines of every kind inside quotes
    b'a,b\n"unterminated,x\n',
    b'a,b\n"x"y,z\n',
    b'a,b\nx"y,z\n',
    b'a,b\n"a""\n',
    b'a,b\n"a"",x"\n',
    b"=SUM(A1),+1,-2,@x\n",
    b"a,b\nx\x00y,z\n",
    b"",
    b"\xef\xbb\xbf",
    b"   ,  \n \t, \xc2\xa0\n",
]


def _legacy(source: bytes):
    try:
        doc = LegacyCsvParser()._parse(source)
    except LegacyError as error:
        return ("error", error.code)
    return ("ok", doc.encoding, doc.bom, doc.delimiter, doc.quotechar, doc.line_ending, doc.mixed_line_endings,
            tuple(doc.row_lengths), tuple((c.row, c.column, c.start, c.end, c.raw, c.native, c.value, c.quoted) for c in doc.cells))


def _new(source: bytes):
    try:
        doc = CsvAdapter().document(source)
    except CsvError as error:
        return ("error", error.code)
    return ("ok", doc.encoding, doc.bom, doc.delimiter, doc.quotechar, doc.line_ending, doc.mixed_line_endings,
            tuple(doc.row_lengths), tuple((c.row, c.column, c.start, c.end, c.raw, c.native, c.value, c.quoted) for c in doc.cells))


@pytest.mark.parametrize("source", HANDWRITTEN + [p.read_bytes() for p in FIXTURES],
                         ids=[f"hand{i}" for i in range(len(HANDWRITTEN))] + [p.name for p in FIXTURES])
def test_compact_parser_matches_legacy_parser(source):
    assert _new(source) == _legacy(source)


def test_compact_parser_matches_legacy_parser_on_random_inputs():
    rng = random.Random(20261109)
    alphabet = [b"a", b"b", b"1", b" ", b",", b",", b'"', b'"', b"\n", b"\r\n", b"\r", b";", b"\t",
                "é".encode(), "€".encode(), b"\xe9", b"\x00", b"=", b"-"]
    mismatches = []
    for _ in range(20000):
        source = b"".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        if rng.random() < 0.2:
            source = b"\xef\xbb\xbf" + source
        legacy = None
        try:
            legacy = _legacy(source)
        except (UnicodeDecodeError, ValueError) as error:  # legacy crashed outside CsvError (BOM + invalid UTF-8)
            legacy = ("crash", type(error).__name__)
        new = _new(source)
        if _bom_with_invalid_utf8(source):
            # Intentional Phase 2B tightening: the legacy parser never validated a BOM file's payload and
            # either crashed or reported an unrelated structural error. It never accepted such a file.
            if new != ("error", "unsupported_encoding") or legacy[0] == "ok":
                mismatches.append((source, legacy, new))
        elif new != legacy:
            mismatches.append((source, legacy, new))
    assert mismatches == []


def _bom_with_invalid_utf8(source: bytes) -> bool:
    if not source.startswith(b"\xef\xbb\xbf"):
        return False
    try:
        source[3:].decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def test_index_lookups_are_exact_and_constant_shape():
    source = b'h1,h2,h3\n1,"a,b",3\n\n4,5\n6,7,8,9\n'
    doc = CsvAdapter().document(source)
    assert tuple(doc.row_lengths) == (3, 3, 0, 2, 4)
    assert doc.cell(2, 2).value == "a,b" and doc.cell(3, 1) is None and doc.cell(4, 3) is None
    assert doc.cell(5, 4).value == "9" and doc.cell(6, 1) is None and doc.cell(0, 1) is None
    for index, cell in enumerate(doc.cells):
        assert doc.address_of(index) == (cell.row, cell.column)
        assert doc.cell(cell.row, cell.column) == cell
    assert list(doc.iter_cells(4, 7)) == list(doc.cells)[4:7]


def test_windows_and_lookup_match_the_full_target_list():
    rows = [b"id,name,amount,note"] + [f'CLM-{i},"Name, {i}",{i}.50,"x""{i}"'.encode() for i in range(1, 900)]
    source = b"\r\n".join(rows) + b"\r\n"
    adapter = CsvAdapter()
    full = adapter.targets(source)
    assert adapter.target_count(source) == len(full) == 3600
    assert adapter.target_window(source, 1990, 25) == full[1990:2015]
    assert adapter.target_window(source, 3590, 2000) == full[3590:]
    wanted = ["csv:900:4", "csv:2:2", "csv:901:1", "csv:0:1", "xlsx:A1", "csv:2:2", "csv:02:2"]
    found = adapter.targets_by_id(source, wanted)
    assert [t.id for t in found] == ["csv:2:2", "csv:900:4"]
    assert found == [t for t in full if t.id in {"csv:2:2", "csv:900:4"}]
    deep = found[1]
    assert deep.fingerprint == hashlib.sha256(b'"x""899"').hexdigest() and deep.value.value == 'x"899'
    assert deep.label == "note"


def test_parse_is_reused_for_the_same_source_object_only():
    adapter = CsvAdapter()
    source = b"a,b\n1,2\n"
    first = adapter.document(source)
    assert adapter.document(source) is first
    copy = bytes(bytearray(source))
    assert adapter.document(copy) is not first
    assert CsvAdapter().document(source) is not first


def test_resource_limits_reject_with_stable_reason_codes(monkeypatch):
    import packages.core.adapters.csv_adapter.adapter as module
    monkeypatch.setattr(module, "MAX_CSV_CELLS", 5)
    profile = CsvAdapter().inspect(b"a,b,c\n1,2,3\n")
    assert (profile.level, profile.reasons) == ("rejected", ("csv_cell_limit_exceeded",))
    monkeypatch.setattr(module, "MAX_CSV_CELLS", MAX_CSV_CELLS)
    monkeypatch.setattr(module, "MAX_CSV_ROWS", 3)
    profile = CsvAdapter().inspect(b"a\nb\nc\nd\n")
    assert (profile.level, profile.reasons) == ("rejected", ("csv_row_limit_exceeded",))
    assert MAX_CSV_ROWS >= 1_000_000 and MAX_CSV_CELLS >= 2_000_000
