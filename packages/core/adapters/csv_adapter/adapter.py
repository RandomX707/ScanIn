from __future__ import annotations

import codecs
import csv
import hashlib
import re
from array import array
from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from typing import Any

from packages.core.models.contracts import CapabilityProfile, ChangePermission, EditTarget, PatchPlan, TypedValue


class CsvError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# Resource limits for one CSV document. They bound the compact index (about 18 bytes per cell and
# 16 bytes per row) and the single scan, so a 50 MiB upload cannot exhaust the restricted worker.
# Inputs above a limit are rejected with a stable reason code; nothing is silently truncated.
MAX_CSV_CELLS = 3_000_000
MAX_CSV_ROWS = 1_000_000

_SPACE_BYTES = bytes(byte for byte in range(128) if chr(byte).isspace())


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


class _CellView(Sequence):
    """Read-only, lazily materialized view of every cell in source order (old ``cells`` tuple API)."""

    def __init__(self, document: "CsvDocument"):
        self._document = document

    def __len__(self) -> int:
        return self._document.cell_count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self._document.cell_at(item) for item in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return self._document.cell_at(index)

    def __iter__(self) -> Iterator[CsvCell]:
        return self._document.iter_cells()


class CsvDocument:
    """Compact, offset-indexed CSV representation.

    Only byte offsets are stored per cell (start, end, quoted flag); values, native text and
    fingerprints are produced on demand from the immutable source bytes. ``cell(row, column)`` is an
    O(1) lookup through per-row cell offsets, so exact target access never scans the document.
    """

    __slots__ = ("encoding", "bom", "delimiter", "quotechar", "line_ending", "mixed_line_endings",
                 "source", "row_lengths", "_row_first", "_starts", "_ends", "_quoted", "_nonempty_rows", "cells")

    def __init__(self, *, encoding: str, bom: bytes, delimiter: int, quotechar: int, line_ending: bytes | None,
                 mixed_line_endings: bool, source: bytes, row_lengths: array, row_first: array,
                 starts: array, ends: array, quoted: bytearray):
        self.encoding, self.bom, self.delimiter, self.quotechar = encoding, bom, delimiter, quotechar
        self.line_ending, self.mixed_line_endings, self.source = line_ending, mixed_line_endings, source
        self.row_lengths, self._row_first = row_lengths, row_first
        self._starts, self._ends, self._quoted = starts, ends, quoted
        self._nonempty_rows: int | None = None
        self.cells = _CellView(self)

    @property
    def cell_count(self) -> int:
        return len(self._starts)

    def index_of(self, row: int, column: int) -> int | None:
        if row < 1 or column < 1 or row > len(self.row_lengths) or column > self.row_lengths[row - 1]:
            return None
        return self._row_first[row - 1] + column - 1

    def cell(self, row: int, column: int) -> CsvCell | None:
        index = self.index_of(row, column)
        return None if index is None else self._cell(index, row, column)

    def address_of(self, index: int) -> tuple[int, int]:
        # The last row whose first cell index is <= index owns it (empty rows before it share its
        # first index and sort earlier, so bisect_right always lands on the owning row).
        row = bisect_right(self._row_first, index, 0, len(self.row_lengths))
        return row, index - self._row_first[row - 1] + 1

    def cell_at(self, index: int) -> CsvCell:
        row, column = self.address_of(index)
        return self._cell(index, row, column)

    def iter_cells(self, start: int = 0, stop: int | None = None) -> Iterator[CsvCell]:
        stop = self.cell_count if stop is None else min(stop, self.cell_count)
        if start >= stop:
            return
        row, column = self.address_of(start)
        index = start
        while index < stop:
            while column > self.row_lengths[row - 1]:
                row, column = row + 1, 1
            yield self._cell(index, row, column)
            index += 1
            column += 1

    def raw(self, index: int) -> bytes:
        return self.source[self._starts[index]:self._ends[index]]

    def _content(self, index: int) -> bytes:
        raw = self.raw(index)
        if self._quoted[index]:
            quote = bytes((self.quotechar,))
            return raw[1:-1].replace(quote + quote, quote)
        return raw

    def _cell(self, index: int, row: int, column: int) -> CsvCell:
        raw = self.raw(index)
        # The whole payload was strictly decoded during parsing and every cell boundary is an ASCII
        # byte, so each cell decodes strictly on its own.
        return CsvCell(row, column, self._starts[index], self._ends[index], raw, raw.decode(self.encoding),
                       self._content(index).decode(self.encoding), bool(self._quoted[index]))

    def nonempty_row_count(self) -> int:
        """Rows with at least one cell whose value is not blank after ``str.strip()``; one pass, cached."""
        if self._nonempty_rows is None:
            count = 0
            for row, length in enumerate(self.row_lengths, start=1):
                first = self._row_first[row - 1]
                for index in range(first, first + length):
                    content = self._content(index).translate(None, _SPACE_BYTES)
                    if content and (content.isascii() or self._content(index).decode(self.encoding).strip()):
                        count += 1
                        break
            self._nonempty_rows = count
        return self._nonempty_rows


class CsvAdapter:
    name = "csv"
    version = "0.1.0"
    _numeric = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")

    def inspect(self, source: bytes) -> CapabilityProfile:
        try:
            doc = self._parse(source)
        except CsvError as error:
            level = "read_only" if error.code in {"unsupported_encoding", "mixed_line_endings", "unsupported_dialect"} else "rejected"
            return CapabilityProfile(level, (error.code,), (), (), self.name, self.version)
        features = {"utf8_bom" if doc.bom else doc.encoding, self._delimiter_name(doc.delimiter), self._line_name(doc.line_ending)}
        blocking = ("mixed_line_endings",) if doc.mixed_line_endings else ()
        # Agreed policy: bytes that are not UTF-8 but decode strictly as Windows-1252 stay editable,
        # but the guess is always reported so the OWNER sees it before any edit is prepared.
        notices = ("encoding_guessed_cp1252",) if doc.encoding == "cp1252" else ()
        return CapabilityProfile("read_only" if blocking else "editable", blocking + notices,
                                 tuple(sorted(features)), (), self.name, self.version)

    def __init__(self) -> None:
        # Single-entry, per-instance memo of the last parsed source. Adapter instances live for one
        # worker or API request, the key is the immutable bytes object itself (identity), and nothing is
        # shared between instances, so no parse result can outlive its request or cross tenants.
        self._memo: tuple[bytes, CsvDocument] | None = None

    def document(self, source: bytes) -> CsvDocument:
        """Parse once per source object; later calls on the same bytes reuse the compact index."""
        memo = self._memo
        if memo is not None and memo[0] is source:
            return memo[1]
        document = self._scan(source)
        self._memo = (source, document)
        return document

    _parse = document

    def target_count(self, source: bytes) -> int:
        return self.document(source).cell_count

    def target_window(self, source: bytes, offset: int, limit: int) -> list[EditTarget]:
        """Targets for cells [offset, offset + limit) in source order (editable or unsupported)."""
        doc = self.document(source)
        if offset < 0 or limit < 0:
            raise CsvError("invalid_target_window")
        return [self._target(doc, cell) for cell in doc.iter_cells(offset, offset + limit)]

    def targets_by_id(self, source: bytes, target_ids) -> list[EditTarget]:
        """Exact lookup in source order; unknown or malformed identifiers are simply absent."""
        doc = self.document(source)
        found: dict[int, CsvCell] = {}
        for target_id in target_ids:
            try:
                row, column = self._address(target_id)
            except CsvError:
                continue
            index = doc.index_of(row, column)
            if index is not None and target_id == f"csv:{row}:{column}":
                found[index] = doc.cell_at(index)
        return [self._target(doc, found[index]) for index in sorted(found)]

    def lookup_targets(self, source: bytes, target_ids) -> tuple[dict[str, EditTarget], dict[str, EditTarget]]:
        """Planner hook: (editable targets, unsupported targets) for exactly the requested ids."""
        editable: dict[str, EditTarget] = {}
        unsupported: dict[str, EditTarget] = {}
        for target in self.targets_by_id(source, target_ids):
            (editable if target.editable else unsupported)[target.id] = target
        return editable, unsupported

    def _target(self, doc: CsvDocument, cell: CsvCell) -> EditTarget:
        version_id = self._version_id(doc)
        header = doc.cell(1, cell.column) if cell.row > 1 else None
        label = header.value if header and header.value else None
        if doc.mixed_line_endings:
            return EditTarget(f"csv:{cell.row}:{cell.column}", version_id, "", f"{cell.row},{cell.column}",
                              self._typed(cell), hashlib.sha256(cell.raw).hexdigest(), None, False,
                              "mixed_line_endings", (), label)
        return EditTarget(f"csv:{cell.row}:{cell.column}", version_id, "", f"{cell.row},{cell.column}",
                          self._typed(cell), hashlib.sha256(cell.raw).hexdigest(), None, True, None, (), label)

    def _version_id(self, doc: CsvDocument) -> str:
        cached = getattr(self, "_version_cache", None)
        if cached is not None and cached[0] is doc.source:
            return cached[1]
        digest = hashlib.sha256(doc.source).hexdigest()
        self._version_cache = (doc.source, digest)
        return digest

    def targets(self, source: bytes) -> list[EditTarget]:
        doc = self.document(source)
        if doc.mixed_line_endings:
            return []
        return [self._target(doc, cell) for cell in doc.iter_cells()]

    def unsupported_targets(self, source: bytes) -> list[EditTarget]:
        doc = self.document(source)
        if not doc.mixed_line_endings:
            return []
        return [self._target(doc, cell) for cell in doc.iter_cells()]

    def target_at(self, source: bytes, selection: dict[str, Any]) -> EditTarget | None:
        try:
            target_id = f"csv:{int(selection['row'])}:{int(selection['column'])}"
        except (KeyError, TypeError, ValueError):
            return None
        return next((item for item in self.targets_by_id(source, [target_id]) if item.editable), None)

    def validate_new_value(self, value: TypedValue) -> None:
        if value.type not in {"string", "integer", "decimal", "date", "datetime", "boolean", "null"}:
            raise CsvError("invalid_type")
        text = self._serialize_value(value)
        if value.type == "decimal" and not Decimal(str(value.value)).is_finite():
            raise CsvError("invalid_number")
        if value.type == "date":
            try:
                date.fromisoformat(str(value.value))
            except ValueError:
                raise CsvError("invalid_date") from None
        if value.type == "datetime":
            try:
                datetime.fromisoformat(str(value.value))
            except ValueError:
                raise CsvError("invalid_datetime") from None
        if value.type == "string" and text.startswith(("=", "+", "-", "@", "\t", "\r")):
            raise CsvError("unsafe_csv_value")

    def formula_dependents(self, source: bytes, target: EditTarget) -> tuple[tuple[str, ...], bool]:
        return (), False

    @staticmethod
    def type_compatible(old: TypedValue, new: TypedValue) -> bool:
        return old.type == new.type or {old.type, new.type} <= {"integer", "decimal"}

    def permission_for(self, source: bytes, target: EditTarget, value: TypedValue,
                       provenance: dict[str, str]) -> tuple[ChangePermission, ...]:
        doc = self._parse(source)
        row, column = self._address(target.id)
        cell = doc.cell(row, column)
        if cell is None or hashlib.sha256(cell.raw).hexdigest() != target.fingerprint:
            raise CsvError("fingerprint_mismatch")
        replacement = self._encode_field(self._serialize_value(value), doc, target_id=target.id, value_type=value.type)
        return (ChangePermission("body", target.id, "replace_value", target.fingerprint,
                                 hashlib.sha256(replacement).hexdigest()),)

    def apply(self, source: bytes, plan: PatchPlan) -> bytes | None:
        if plan.no_change:
            return None
        if hashlib.sha256(source).hexdigest() != plan.source_sha256:
            raise CsvError("stale_source")
        doc = self._parse(source)
        replacements: list[tuple[int, int, bytes]] = []
        for patch in plan.patches:
            row, column = self._address(patch.target_id)
            cell = doc.cell(row, column)
            if cell is None:
                raise CsvError("target_not_found")
            if hashlib.sha256(cell.raw).hexdigest() != patch.expected_fingerprint:
                raise CsvError("fingerprint_mismatch")
            self.validate_new_value(patch.new_value)
            replacements.append((cell.start, cell.end, self._encode_field(self._serialize_value(patch.new_value), doc,
                                                                          target_id=patch.target_id,
                                                                          value_type=patch.new_value.type)))
        out = bytearray(source)
        for start, end, value in sorted(replacements, reverse=True):
            out[start:end] = value
        return bytes(out)

    def parse(self, source: bytes) -> list[list[TypedValue]]:
        doc = self.document(source)
        return [[self._typed(cell) for cell in doc.iter_cells(doc.index_of(row, 1), doc.index_of(row, 1) + length)]
                if length else [] for row, length in enumerate(doc.row_lengths, start=1)]

    def _scan(self, source: bytes) -> CsvDocument:
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
        if bom:
            # A BOM forces UTF-8; the payload must still decode strictly (it is decoded per cell later).
            try:
                payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise CsvError("unsupported_encoding") from None
        delimiter, quotechar = self._dialect(payload, encoding)
        quoted_field, plain_field = _field_patterns(delimiter, quotechar)
        field_limit = csv.field_size_limit()
        starts, ends = array("q"), array("q")
        quoted = bytearray()
        row_lengths, row_first = array("q"), array("q")
        endings: set[bytes] = set()
        length = len(source)
        pos = len(bom)
        row_cells = 0
        row_first.append(0)

        def end_row() -> None:
            nonlocal row_cells
            row_lengths.append(row_cells)
            if len(row_lengths) >= MAX_CSV_ROWS:
                raise CsvError("csv_row_limit_exceeded")
            row_first.append(len(starts))
            row_cells = 0

        while pos < length:
            byte = source[pos]
            if byte == 10 or byte == 13:
                ending = b"\r\n" if byte == 13 and source[pos + 1:pos + 2] == b"\n" else (b"\r" if byte == 13 else b"\n")
                endings.add(ending)
                pos += len(ending)
                end_row()
                continue
            if byte == quotechar:
                match = quoted_field.match(source, pos)
                if match is None:
                    raise CsvError("malformed_csv_quote")
                end = match.end()
                if end < length and source[end] not in (delimiter, 10, 13):
                    raise CsvError("malformed_csv_after_quote")
                is_quoted = 1
            else:
                end = plain_field.match(source, pos).end()
                if end < length and source[end] == quotechar:
                    raise CsvError("malformed_csv_quote")
                is_quoted = 0
            if end - pos > field_limit and self._field_too_large(source[pos:end], is_quoted, quotechar, encoding, field_limit):
                raise CsvError("malformed_csv")
            starts.append(pos)
            ends.append(end)
            quoted.append(is_quoted)
            row_cells += 1
            if len(starts) > MAX_CSV_CELLS:
                raise CsvError("csv_cell_limit_exceeded")
            pos = end
            if pos < length and source[pos] == delimiter:
                pos += 1
                if pos == length:
                    starts.append(pos)
                    ends.append(pos)
                    quoted.append(0)
                    row_cells += 1
        if row_cells:
            row_lengths.append(row_cells)
        else:
            row_first.pop()
        mixed = len(endings) > 1
        ending = None if mixed or not endings else next(iter(endings))
        return CsvDocument(encoding=encoding, bom=bom, delimiter=delimiter, quotechar=quotechar, line_ending=ending,
                           mixed_line_endings=mixed, source=source, row_lengths=row_lengths, row_first=row_first,
                           starts=starts, ends=ends, quoted=quoted)

    @staticmethod
    def _field_too_large(raw: bytes, is_quoted: int, quotechar: int, encoding: str, field_limit: int) -> bool:
        # Same bound the standard csv reader enforced per field before Phase 2B (decoded characters).
        content = raw[1:-1].replace(bytes((quotechar,)) * 2, bytes((quotechar,))) if is_quoted else raw
        return len(content.decode(encoding)) > field_limit

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

    def _typed(self, cell: CsvCell) -> TypedValue:
        text = cell.value
        if not cell.quoted and self._numeric.fullmatch(text):
            if "." not in text:
                try:
                    return TypedValue("integer", int(text), native=text)
                except ValueError:
                    pass
            try:
                return TypedValue("decimal", Decimal(text), native=text)
            except InvalidOperation:
                pass
        return TypedValue("string", text, native=cell.native)

    def _serialize_value(self, value: TypedValue) -> str:
        if value.type == "string":
            return str(value.value)
        if value.type == "integer":
            return str(value.value)
        if value.type == "decimal":
            number = Decimal(str(value.value))
            if not number.is_finite():
                raise CsvError("invalid_number")
            serialized = format(number, "f")
            return serialized if "." in serialized else f"{serialized}.0"
        if value.type in {"date", "datetime"}:
            return str(value.value)
        if value.type == "boolean":
            return "true" if value.value else "false"
        if value.type == "null":
            return ""
        raise CsvError("invalid_type")

    def _encode_field(self, value: str, doc: CsvDocument, *, target_id: str, value_type: str) -> bytes:
        if value_type == "string":
            self.validate_new_value_unchecked(value, target_id)
        quote = chr(doc.quotechar)
        needs_quote = any(char in value for char in (chr(doc.delimiter), quote, "\r", "\n")) or value[:1].isspace() or value[-1:].isspace()
        encoded = quote + value.replace(quote, quote + quote) + quote if needs_quote else value
        try:
            return encoded.encode(doc.encoding, errors="strict")
        except UnicodeEncodeError:
            raise CsvError("value_not_representable_in_encoding") from None

    @staticmethod
    def validate_new_value_unchecked(value: str, target_id: str) -> None:
        if value.startswith(("=", "+", "-", "@", "\t", "\r")):
            raise CsvError("unsafe_csv_value")

    @staticmethod
    def _address(target_id: str) -> tuple[int, int]:
        try:
            prefix, row, column = target_id.split(":")
            if prefix != "csv" or int(row) < 1 or int(column) < 1:
                raise ValueError
            return int(row), int(column)
        except (ValueError, TypeError):
            raise CsvError("invalid_target_id") from None

    @staticmethod
    def _delimiter_name(delimiter: int) -> str:
        return {44: "comma_delimited", 59: "semicolon_delimited", 9: "tab_delimited", 124: "pipe_delimited"}.get(delimiter, "delimited")

    @staticmethod
    def _line_name(ending: bytes | None) -> str:
        return {b"\r\n": "crlf", b"\n": "lf", b"\r": "cr", None: "no_line_ending"}.get(ending, "mixed_line_endings")


_PATTERN_CACHE: dict[tuple[int, int], tuple[re.Pattern[bytes], re.Pattern[bytes]]] = {}


def _field_patterns(delimiter: int, quotechar: int) -> tuple[re.Pattern[bytes], re.Pattern[bytes]]:
    """Compiled field scanners. Possessive quantifiers make the quoted-field match deterministic, exactly
    like the previous byte loop: "" is always an escaped quote and the first lone quote closes the field."""
    key = (delimiter, quotechar)
    if key not in _PATTERN_CACHE:
        q = re.escape(bytes((quotechar,)))
        d = re.escape(bytes((delimiter,)))
        quoted = re.compile(q + b"[^" + q + b"]*+(?:" + q + q + b"[^" + q + b"]*+)*+" + q)
        plain = re.compile(b"[^" + d + q + b"\\r\\n]*+")
        _PATTERN_CACHE[key] = (quoted, plain)
    return _PATTERN_CACHE[key]
