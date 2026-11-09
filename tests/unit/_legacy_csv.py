"""Frozen reference copy of the CSV parser, summary and verifier scanner at f6139a6 (Phase 2A).

Used only by differential tests: the optimized Phase 2B implementation must produce identical cells,
row structure, errors, summaries and verifier spans. Do not import from production code."""
from __future__ import annotations

import codecs
import csv
import io
from dataclasses import dataclass


class CsvError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CsvCell:
    row: int
    column: int
    start: int
    end: int
    raw: bytes
    native: str
    value: str
    quoted: bool


@dataclass(frozen=True)
class CsvDocument:
    encoding: str
    bom: bytes
    delimiter: int
    quotechar: int
    line_ending: bytes | None
    mixed_line_endings: bool
    cells: tuple[CsvCell, ...]
    row_lengths: tuple[int, ...]

    def cell(self, row: int, column: int) -> CsvCell | None:
        return next((cell for cell in self.cells if cell.row == row and cell.column == column), None)


class LegacyCsvParser:
    def _parse(self, source: bytes) -> CsvDocument:
        if not source:
            raise CsvError("empty_csv")
        bom = b"\xef\xbb\xbf" if source.startswith(b"\xef\xbb\xbf") else b""
        payload = source[len(bom):]
        if bom:
            encoding = "utf-8"
        else:
            try:
                payload.decode("utf-8", errors="strict")
                encoding = "utf-8"
            except UnicodeDecodeError:
                try:
                    payload.decode("cp1252", errors="strict")
                    encoding = "cp1252"
                except UnicodeDecodeError:
                    raise CsvError("unsupported_encoding") from None
        delimiter, quotechar = self._dialect(payload, encoding)
        ending = self._line_ending(payload, quotechar=quotechar, delimiter=delimiter)
        mixed = ending == b"mixed"
        start_offset = len(bom)
        cells: list[CsvCell] = []
        row_lengths: list[int] = []
        row, column, pos = 1, 1, start_offset
        row_cells = 0
        while pos < len(source):
            if source[pos] in (10, 13):
                pos = self._consume_line_end(source, pos)
                row_lengths.append(row_cells)
                row += 1
                column = 1
                row_cells = 0
                continue
            field_start = pos
            quoted = source[pos] == quotechar
            if quoted:
                pos += 1
                while pos < len(source):
                    if source[pos] == quotechar:
                        if pos + 1 < len(source) and source[pos + 1] == quotechar:
                            pos += 2
                            continue
                        pos += 1
                        break
                    pos += 1
                else:
                    raise CsvError("malformed_csv_quote")
                if pos < len(source) and source[pos] not in (delimiter, 10, 13):
                    raise CsvError("malformed_csv_after_quote")
            else:
                while pos < len(source) and source[pos] not in (delimiter, 10, 13):
                    pos += 1
                if quotechar in source[field_start:pos]:
                    raise CsvError("malformed_csv_quote")
            raw = source[field_start:pos]
            value = self._decode_field(raw, encoding, delimiter, quotechar)
            try:
                native = raw.decode(encoding, errors="strict")
            except UnicodeDecodeError:
                raise CsvError("unsupported_encoding") from None
            cells.append(CsvCell(row, column, field_start, pos, raw, native, value, quoted))
            row_cells += 1
            if pos < len(source) and source[pos] == delimiter:
                pos += 1
                column += 1
                if pos == len(source):
                    cells.append(CsvCell(row, column, pos, pos, b"", "", "", False))
                    row_cells += 1
            elif pos < len(source) and source[pos] in (10, 13):
                pos = self._consume_line_end(source, pos)
                row_lengths.append(row_cells)
                row += 1
                column = 1
                row_cells = 0
        if row_cells:
            row_lengths.append(row_cells)
        return CsvDocument(encoding, bom, delimiter, quotechar, None if mixed else ending, mixed,
                           tuple(cells), tuple(row_lengths))

    def _dialect(self, payload: bytes, encoding: str) -> tuple[int, int]:
        # Incremental decoding drops only a multi-byte sequence cut by the sample boundary; any
        # genuinely undecodable byte still raises (the whole payload was already decoded strictly).
        sample = codecs.getincrementaldecoder(encoding)(errors="strict").decode(payload[:8192], final=False)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        delimiter, quotechar = dialect.delimiter, dialect.quotechar or '"'
        if len(delimiter) != 1 or len(quotechar) != 1 or ord(delimiter) > 127 or ord(quotechar) > 127:
            raise CsvError("unsupported_dialect")
        return ord(delimiter), ord(quotechar)

    def _decode_field(self, raw: bytes, encoding: str, delimiter: int, quotechar: int) -> str:
        if not raw:
            return ""
        try:
            parsed = list(csv.reader(io.StringIO(raw.decode(encoding), newline=""), delimiter=chr(delimiter),
                                     quotechar=chr(quotechar), doublequote=True, strict=True))
        except (UnicodeDecodeError, csv.Error):
            raise CsvError("malformed_csv") from None
        if len(parsed) != 1 or len(parsed[0]) != 1:
            raise CsvError("malformed_csv")
        return parsed[0][0]

    @staticmethod
    def _line_ending(data: bytes, *, quotechar: int, delimiter: int) -> bytes | None:
        endings: set[bytes] = set()
        quoted = False
        field_start = True
        pos = 0
        while pos < len(data):
            byte = data[pos]
            if quoted:
                if byte == quotechar:
                    if pos + 1 < len(data) and data[pos + 1] == quotechar:
                        pos += 2
                        continue
                    quoted = False
                pos += 1
                continue
            if field_start and byte == quotechar:
                quoted = True
                field_start = False
                pos += 1
                continue
            if byte == delimiter:
                field_start = True
                pos += 1
                continue
            if byte in (10, 13):
                ending = b"\r\n" if data[pos:pos + 2] == b"\r\n" else bytes((byte,))
                endings.add(ending)
                pos += len(ending)
                field_start = True
                continue
            field_start = False
            pos += 1
        if len(endings) > 1:
            return b"mixed"
        if not endings:
            return None
        ending = next(iter(endings))
        return ending

    @staticmethod
    def _consume_line_end(data: bytes, pos: int) -> int:
        if data[pos:pos + 2] == b"\r\n":
            return pos + 2
        return pos + 1



def legacy_verifier_csv_document(data: bytes) -> dict[tuple[int, int], tuple[int, int, bytes, str, bool]]:
    bom = data.startswith(b"\xef\xbb\xbf")
    start = 3 if bom else 0
    payload = data[start:]
    # Independent encoding decision: the whole payload must decode strictly as UTF-8 (always when a
    # BOM is present) or, failing that, as Windows-1252. Nothing is ever decoded with replacement.
    encoding = "utf-8"
    try:
        payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        if bom:
            raise
        encoding = "cp1252"
        payload.decode(encoding, errors="strict")
    sample = codecs.getincrementaldecoder(encoding)(errors="strict").decode(payload[:8192], final=False)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    delimiter, quotechar = ord(dialect.delimiter), ord(dialect.quotechar or '"')
    result: dict[tuple[int, int], tuple[int, int, bytes, str, bool]] = {}
    row = column = 1
    pos = start
    while pos < len(data):
        if data[pos] in (10, 13):
            pos = pos + 2 if data[pos:pos + 2] == b"\r\n" else pos + 1
            row, column = row + 1, 1
            continue
        begin = pos
        quoted = data[pos] == quotechar
        if quoted:
            pos += 1
            while pos < len(data):
                if data[pos] == quotechar:
                    if pos + 1 < len(data) and data[pos + 1] == quotechar:
                        pos += 2
                    else:
                        pos += 1
                        break
                else:
                    pos += 1
        else:
            while pos < len(data) and data[pos] not in (delimiter, 10, 13):
                pos += 1
        raw = data[begin:pos]
        if not raw:
            value = ""
        else:
            parsed = list(csv.reader(io.StringIO(raw.decode(encoding), newline=""), delimiter=chr(delimiter),
                                     quotechar=chr(quotechar), doublequote=True, strict=True))
            if len(parsed) != 1 or len(parsed[0]) != 1:
                raise ValueError("csv_token_invalid")
            value = parsed[0][0]
        result[(row, column)] = (begin, pos, raw, value, quoted)
        if pos < len(data) and data[pos] == delimiter:
            pos += 1
            column += 1
        elif pos < len(data) and data[pos] in (10, 13):
            pos = pos + 2 if data[pos:pos + 2] == b"\r\n" else pos + 1
            row, column = row + 1, 1
    return result




def legacy_csv_summary_facts(source: bytes) -> dict:
    """The f6139a6 CSV branch of StructuralSummaryProvider._summarize, returning its computed facts."""
    from packages.core.summaries.structural import _insurance_type, _looks_data_like
    limitations: list[str] = []
    observed: list[dict[str, str]] = []
    evidence: list[dict[str, str]] = []
    document = LegacyCsvParser()._parse(source)
    nonempty_rows = sum(1 for row in range(1, len(document.row_lengths) + 1)
                        if any(cell.row == row and cell.value.strip() for cell in document.cells))
    width = max(document.row_lengths, default=0)
    headers = [(cell.value.strip() if cell else "") for col in range(1, width + 1)
               for cell in (document.cell(1, col),)]
    has_header = bool(headers and any(not _looks_data_like(value) for value in headers))
    record_count = max(0, nonempty_rows - (1 if has_header else 0))
    record_word = "record" if record_count == 1 else "records"
    stats = {"record_count": record_count, "column_count": width}
    names = [(name or f"Column {index}") for index, name in enumerate(headers, 1)] if has_header else [f"Column {index}" for index in range(1, width + 1)]
    if has_header and len(document.row_lengths) > 1 and any(length != document.row_lengths[0] for length in document.row_lengths[1:]):
        limitations.append("Rows have different column counts; unnamed columns are labeled by position.")
    for index, name in enumerate(names, start=1):
        if not name:
            continue
        observed.append({"name": name[:100], "kind": "column"})
        evidence.append({"reference": f"csv:column:{index}", "label": name[:100]})
    apparent_type = _insurance_type(headers if has_header else [])
    fields = ", ".join(names[:8]) or "unnamed columns"
    description = f"This CSV contains {record_count} data {record_word} across {stats['column_count']} columns. Its column headings include {fields}."
    if apparent_type:
        description += f" The field names are consistent with an {apparent_type.replace(' document', '')} dataset."
    else:
        description += " The document purpose is unclear from its column headings alone."
    return {"stats": stats, "observed": observed, "evidence": evidence, "limitations": limitations,
            "apparent_type": apparent_type, "description": description}
