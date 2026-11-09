from __future__ import annotations

import codecs
import csv
import hashlib
import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from typing import Any

from packages.core.models.contracts import CapabilityProfile, ChangePermission, EditTarget, PatchPlan, TypedValue


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

    def targets(self, source: bytes) -> list[EditTarget]:
        doc = self._parse(source)
        if doc.mixed_line_endings:
            return []
        version_id = hashlib.sha256(source).hexdigest()
        result: list[EditTarget] = []
        for cell in doc.cells:
            value = self._typed(cell)
            target_id = f"csv:{cell.row}:{cell.column}"
            header = doc.cell(1, cell.column)
            label = header.value if header and cell.row > 1 and header.value else None
            result.append(EditTarget(target_id, version_id, "", f"{cell.row},{cell.column}", value,
                                     hashlib.sha256(cell.raw).hexdigest(), None, True, None, (), label))
        return result

    def unsupported_targets(self, source: bytes) -> list[EditTarget]:
        doc = self._parse(source)
        if not doc.mixed_line_endings:
            return []
        version_id = hashlib.sha256(source).hexdigest()
        return [EditTarget(f"csv:{cell.row}:{cell.column}", version_id, "", f"{cell.row},{cell.column}",
                           self._typed(cell), hashlib.sha256(cell.raw).hexdigest(), None, False,
                           "mixed_line_endings", (), doc.cell(1, cell.column).value if cell.row > 1 and doc.cell(1, cell.column) else None)
                for cell in doc.cells]

    def target_at(self, source: bytes, selection: dict[str, Any]) -> EditTarget | None:
        try:
            target_id = f"csv:{int(selection['row'])}:{int(selection['column'])}"
        except (KeyError, TypeError, ValueError):
            return None
        return next((item for item in self.targets(source) if item.id == target_id), None)

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
        doc = self._parse(source)
        return [[self._typed(cell) for cell in doc.cells if cell.row == row]
                for row in range(1, len(doc.row_lengths) + 1)]

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
