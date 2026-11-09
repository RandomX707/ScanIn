from __future__ import annotations

import hashlib
import io
import json
import math
import posixpath
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from lxml import etree
from openpyxl.styles.numbers import is_date_format
from openpyxl.utils.datetime import WINDOWS_EPOCH, MAC_EPOCH, from_excel, to_excel
from openpyxl.utils.cell import range_boundaries, get_column_letter

from packages.core.models.contracts import CapabilityProfile, ChangePermission, EditTarget, Patch, PatchPlan, TypedValue

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT = "http://schemas.openxmlformats.org/package/2006/content-types"
CUSTOM = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
NS = {"m": MAIN, "r": DOC_REL, "p": PKG_REL, "ct": CONTENT}
CUSTOM_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"
CUSTOM_TYPE = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
PROVENANCE_NAME = "IDW_M0_Provenance"


class XlsxError(ValueError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


def _parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False, huge_tree=False)


def _xml(data: bytes) -> etree._Element:
    return etree.fromstring(data, parser=_parser())


def _c14n(node: etree._Element) -> bytes:
    return etree.tostring(node, method="c14n", exclusive=False, with_comments=True)


def _fingerprint(node: etree._Element) -> str:
    # Canonicalise a detached copy so namespace context cannot change the digest.
    detached = _xml(etree.tostring(node))
    return hashlib.sha256(_c14n(detached)).hexdigest()


def _zip_parts(source: bytes) -> dict[str, tuple[zipfile.ZipInfo, bytes]]:
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            bad = archive.testzip()
            if bad:
                raise XlsxError("corrupt_zip", bad)
            return {info.filename: (info, archive.read(info.filename)) for info in archive.infolist()}
    except XlsxError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise XlsxError("invalid_or_encrypted_xlsx", str(exc)) from exc


def _workbook_map(parts: dict[str, tuple[zipfile.ZipInfo, bytes]]) -> tuple[dict[str, str], etree._Element]:
    if "xl/workbook.xml" not in parts or "xl/_rels/workbook.xml.rels" not in parts:
        raise XlsxError("not_xlsx")
    wb = _xml(parts["xl/workbook.xml"][1])
    rels = _xml(parts["xl/_rels/workbook.xml.rels"][1])
    targets = {r.get("Id"): r.get("Target", "") for r in rels.xpath("./p:Relationship", namespaces=NS)}
    sheets: dict[str, str] = {}
    for sh in wb.xpath("./m:sheets/m:sheet", namespaces=NS):
        target = targets.get(sh.get(f"{{{DOC_REL}}}id"), "")
        path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
        sheets[sh.get("name", "")] = path
    return sheets, wb


def _shared_strings(parts: dict[str, tuple[zipfile.ZipInfo, bytes]]) -> tuple[list[str], set[int]]:
    if "xl/sharedStrings.xml" not in parts:
        return [], set()
    root = _xml(parts["xl/sharedStrings.xml"][1])
    strings: list[str] = []
    rich: set[int] = set()
    for index, si in enumerate(root.xpath("./m:si", namespaces=NS)):
        runs = si.xpath("./m:r", namespaces=NS)
        if runs:
            rich.add(index)
        strings.append("".join(si.xpath(".//m:t/text()", namespaces=NS)))
    return strings, rich


def _date_styles(parts: dict[str, tuple[zipfile.ZipInfo, bytes]]) -> set[int]:
    if "xl/styles.xml" not in parts:
        return set()
    styles = _xml(parts["xl/styles.xml"][1])
    custom: dict[int, str] = {}
    for n in styles.xpath("./m:numFmts/m:numFmt", namespaces=NS):
        try:
            custom[int(n.get("numFmtId", "0"))] = n.get("formatCode", "")
        except ValueError:
            continue
    ids: set[int] = set()
    for i, xf in enumerate(styles.xpath("./m:cellXfs/m:xf", namespaces=NS)):
        try:
            fmt = int(xf.get("numFmtId", "0"))
        except ValueError:
            continue
        if 14 <= fmt <= 22 or 27 <= fmt <= 36 or 45 <= fmt <= 47 or 50 <= fmt <= 58 or is_date_format(custom.get(fmt, "")):
            ids.add(i)
    return ids


def _cell_value(cell: etree._Element, shared: list[str], rich: set[int], dates: set[int], epoch) -> tuple[TypedValue | None, str | None]:
    f = cell.find(f"{{{MAIN}}}f")
    if f is not None:
        return None, "array_formula" if f.get("t") in {"array", "dataTable"} else "formula_cell"
    ctype = cell.get("t")
    raw = cell.findtext(f"{{{MAIN}}}v")
    if ctype == "s":
        try:
            idx = int(raw or "")
            if idx in rich:
                return None, "rich_text"
            return TypedValue("string", shared[idx], native=raw), None
        except (ValueError, IndexError):
            return None, "invalid_shared_string"
    if ctype == "inlineStr":
        inline = cell.find(f"{{{MAIN}}}is")
        if inline is None:
            return TypedValue("string", "", native=""), None
        if inline.xpath("./m:r", namespaces=NS):
            return None, "rich_text"
        inline_text = "".join(inline.xpath(".//m:t/text()", namespaces=NS))
        return TypedValue("string", inline_text, native=inline_text), None
    if ctype == "str":
        return TypedValue("string", raw or "", native=raw or ""), None
    if ctype == "b":
        return TypedValue("boolean", raw == "1", native=raw), None
    if ctype == "e":
        return None, "error_cell"
    if ctype == "d":
        try:
            return TypedValue("date", date.fromisoformat(raw or "").isoformat(), native=raw), None
        except ValueError:
            return None, "invalid_date"
    if raw is None:
        return None, "blank_cell"
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None, "invalid_number"
    if not number.is_finite():
        return None, "invalid_number"
    style = int(cell.get("s", "0")) if cell.get("s", "0").isdigit() else 0
    if style in dates:
        parsed = from_excel(float(number), epoch=epoch)
        as_date = parsed.date().isoformat() if isinstance(parsed, datetime) else parsed.isoformat()
        return TypedValue("date", as_date, native=raw), None
    if number == number.to_integral_value() and "e" not in raw.lower() and "." not in raw:
        return TypedValue("integer", int(number), native=raw), None
    return TypedValue("decimal", float(number), native=raw), None


CELL_REF = re.compile(r"(?:(?:'((?:[^']|'')+)'|([A-Za-z_][A-Za-z0-9_. ]*))!)?(\$?[A-Z]{1,3}\$?[1-9][0-9]*)(?::(\$?[A-Z]{1,3}\$?[1-9][0-9]*))?", re.I)
UNSAFE_FORMULA = re.compile(r"\b(?:INDIRECT|OFFSET)\s*\(|\[[^\]]+\]|\bR\[?-?\d", re.I)
CELL_COORD = re.compile(r"^\$?([A-Z]{1,3})\$?([1-9][0-9]*)$", re.I)


def _column_index(text: str) -> int:
    out = 0
    for char in text.upper():
        out = out * 26 + ord(char) - 64
    return out


def _formula_references(formula: str, own_sheet: str) -> tuple[list[tuple[str, str, str]], bool]:
    clean = re.sub(r'"(?:[^"]|"")*"', '""', formula)
    if UNSAFE_FORMULA.search(clean):
        return [], True
    matches = list(CELL_REF.finditer(clean))
    refs: list[tuple[str, str, str]] = []
    for m in matches:
        sheet = (m.group(1).replace("''", "'") if m.group(1) else m.group(2) or own_sheet).strip()
        refs.append((sheet, m.group(3), m.group(4) or m.group(3)))
    residue = CELL_REF.sub("", clean)
    # Defined names are supported only when mapped to a range; other bare names make analysis unknown.
    residue = re.sub(r"(?:'[^']+'|[A-Za-z_][A-Za-z0-9_. ]*)!", "", residue)
    residue = re.sub(r"\b(?:SUM|AVERAGE|MIN|MAX|COUNT|IF|AND|OR|NOT|ROUND|ABS|COUNTIF|SUMIF)\b", "", residue, flags=re.I)
    residue = re.sub(r"\b(?:TRUE|FALSE)\b", "", residue, flags=re.I)
    residue = re.sub(r"[0-9.]+|[+*/^<>=(),;:%&\s-]+", "", residue)
    # Conservative: defined names, table syntax, or other identifiers are not resolved in this M0 parser.
    unknown = bool(residue)
    return refs, unknown


def _defined_names(wb: etree._Element, sheets: dict[str, str]) -> dict[str, str]:
    names: dict[str, str] = {}
    ordered = list(sheets)
    for node in wb.xpath("./m:definedNames/m:definedName", namespaces=NS):
        name = node.get("name", "")
        formula = (node.text or "").lstrip("=")
        if not name or not CELL_REF.fullmatch(formula):
            continue
        local = node.get("localSheetId")
        if local is None:
            names[name.lower()] = formula
        elif local.isdigit() and int(local) < len(ordered):
            names[f"{ordered[int(local)].lower()}!{name.lower()}"] = formula
    return names


def _resolve_names(formula: str, own_sheet: str, names: dict[str, str]) -> str:
    clean = re.sub(r'"(?:[^"]|"")*"', '""', formula)
    for name in sorted(names, key=len, reverse=True):
        if "!" in name:
            scope, bare = name.split("!", 1)
        else:
            scope, bare = "", name
        if scope and scope != own_sheet.lower():
            continue
        clean = re.sub(rf"(?<![A-Za-z0-9_.]){re.escape(bare)}(?![A-Za-z0-9_.])", f"({names[name]})", clean, flags=re.I)
    return clean


def _unlocked_styles(parts: dict[str, tuple[zipfile.ZipInfo, bytes]]) -> set[int]:
    if "xl/styles.xml" not in parts:
        return set()
    styles = _xml(parts["xl/styles.xml"][1])
    unlocked: set[int] = set()
    for index, xf in enumerate(styles.xpath("./m:cellXfs/m:xf", namespaces=NS)):
        protection = xf.find(f"{{{MAIN}}}protection")
        if protection is not None and protection.get("locked", "1").lower() in {"0", "false"}:
            unlocked.add(index)
    return unlocked


class XlsxAdapter:
    name = "xlsx"
    version = "0.1.0"

    def inspect(self, source: bytes) -> CapabilityProfile:
        if source.startswith(b"\xd0\xcf\x11\xe0"):
            return CapabilityProfile("rejected", ("encrypted_or_legacy_office",), (), (), self.name, self.version)
        try:
            parts = _zip_parts(source)
        except XlsxError as exc:
            return CapabilityProfile("rejected", (exc.code,), (), (), self.name, self.version)
        sheets, wb = _workbook_map(parts)
        names = set(parts)
        features: set[str] = {"styles"} if "xl/styles.xml" in parts else set()
        if "xl/sharedStrings.xml" in parts:
            features.add("shared_strings")
        if any(n.startswith("xl/charts/") for n in names): features.add("charts")
        if any(n.startswith("xl/media/") for n in names): features.add("images")
        if any(n.startswith("xl/externalLinks/") for n in names): features.add("external_links")
        if any("pivotCache" in n for n in names): features.add("pivot_caches")
        if any("slicer" in n.lower() for n in names): features.add("slicers")
        if any("embeddings/" in n for n in names): features.add("embedded_objects")
        if any(n.startswith("xl/comments") or "/comments" in n for n in names): features.add("comments")
        if any(n.startswith("xl/drawings/") for n in names): features.add("drawings")
        if any(n.startswith("xl/worksheets/") for n in names):
            if wb.xpath("./m:definedNames/m:definedName", namespaces=NS): features.add("defined_names")
            for part, (_, data) in parts.items():
                if part.startswith("xl/worksheets/") and part.endswith(".xml"):
                    root = _xml(data)
                    if root.xpath(".//m:f", namespaces=NS): features.add("formulas")
                    if root.xpath(".//m:dataValidations", namespaces=NS): features.add("data_validations")
                    if root.xpath(".//m:conditionalFormatting", namespaces=NS): features.add("conditional_formatting")
        content_types = parts.get("[Content_Types].xml", (None, b""))[1]
        macro_type = b"macroEnabled" in content_types
        if "xl/vbaProject.bin" in names or macro_type:
            return CapabilityProfile("read_only", ("macros_present",), tuple(sorted(features)), tuple(sheets), self.name, self.version)
        reasons: list[str] = []
        if "external_links" in features: reasons.append("external_links_present")
        if "pivot_caches" in features: reasons.append("pivot_caches_present")
        if "slicers" in features: reasons.append("slicers_present")
        if "embedded_objects" in features: reasons.append("embedded_objects_present")
        if any(n.startswith("xl/externalLinks/") for n in names):
            # External links make formula results untrustworthy; file remains editable with incomplete dependencies as needed.
            pass
        if "encryptedPackage" in names or "EncryptionInfo" in names:
            return CapabilityProfile("rejected", ("encrypted",), tuple(sorted(features)), tuple(sheets), self.name, self.version)
        return CapabilityProfile("editable", tuple(reasons), tuple(sorted(features)), tuple(sheets), self.name, self.version)

    def _data(self, source: bytes):
        parts = _zip_parts(source)
        sheets, wb = _workbook_map(parts)
        shared, rich = _shared_strings(parts)
        dates = _date_styles(parts)
        date1904 = wb.find(f"{{{MAIN}}}workbookPr") is not None and wb.find(f"{{{MAIN}}}workbookPr").get("date1904", "0").lower() in {"1", "true"}
        epoch = MAC_EPOCH if date1904 else WINDOWS_EPOCH
        formulas: list[tuple[str, etree._Element]] = []
        for sheet, part in sheets.items():
            root = _xml(parts[part][1])
            for c in root.xpath(".//m:c", namespaces=NS):
                f = c.find(f"{{{MAIN}}}f")
                if f is not None:
                    formulas.append((sheet, c))
        unknown_formula = False
        refs: list[tuple[str, str, str, str]] = []
        name_map = _defined_names(wb, sheets)
        for sheet, c in formulas:
            f = c.find(f"{{{MAIN}}}f")
            parsed, unknown = _formula_references(_resolve_names(f.text or "", sheet, name_map), sheet)
            unknown_formula |= unknown
            refs.extend((target_sheet, first, last, f"{sheet}!{c.get('r')}") for target_sheet, first, last in parsed)
        return parts, sheets, shared, rich, dates, epoch, formulas, refs, unknown_formula

    def targets(self, source: bytes) -> list[EditTarget]:
        parts, sheets, shared, rich, dates, epoch, formulas, refs, unknown_formula = self._data(source)
        unlocked_styles = _unlocked_styles(parts)
        formula_cells = {f"{sheet}!{c.get('r')}" for sheet, c in formulas}
        result: list[EditTarget] = []
        for sheet, part in sheets.items():
            root = _xml(parts[part][1])
            sheet_protected = root.find(f"{{{MAIN}}}sheetProtection") is not None
            for cell in root.xpath(".//m:c", namespaces=NS):
                value, reason = _cell_value(cell, shared, rich, dates, epoch)
                if value is None or reason:
                    continue
                ref = cell.get("r", "")
                style_id = int(cell.get("s", "0")) if cell.get("s", "0").isdigit() else 0
                if sheet_protected and style_id not in unlocked_styles:
                    reason = "protected_cell_locked"
                validation_options, validation_unsupported = self._list_validation_options(
                    parts, sheets, shared, rich, dates, epoch, sheet, ref, root)
                if validation_unsupported:
                    reason = reason or "unsupported_data_validation"
                deps: set[str] = set()
                if not unknown_formula:
                    for target_sheet, first, last, formula_id in refs:
                        if target_sheet != sheet:
                            continue
                        a, b = CELL_COORD.match(first), CELL_COORD.match(last)
                        x = CELL_COORD.match(ref)
                        if not (a and b and x): continue
                        col, row = _column_index(x.group(1)), int(x.group(2))
                        if _column_index(a.group(1)) <= col <= _column_index(b.group(1)) and int(a.group(2)) <= row <= int(b.group(2)):
                            deps.add(formula_id)
                fp = _fingerprint(cell)
                result.append(EditTarget(f"xlsx:{sheet}!{ref}", hashlib.sha256(source).hexdigest(), sheet, ref,
                                         value, fp, cell.get("s"), reason is None, reason,
                                         None if unknown_formula else tuple(sorted(deps))))
        return result

    def _list_validation_options(self, parts, sheets, shared, rich, dates, epoch,
                                 sheet_name: str, cell_ref: str, root) -> tuple[set[str] | None, bool]:
        options: set[str] | None = None
        unsupported = False
        wb = _xml(parts["xl/workbook.xml"][1])
        names = _defined_names(wb, sheets)
        for validation in root.xpath(".//m:dataValidation", namespaces=NS):
            if validation.get("type") != "list":
                continue
            applies = False
            for area in (validation.get("sqref") or "").split():
                try:
                    min_col, min_row, max_col, max_row = range_boundaries(area)
                    match = CELL_COORD.match(cell_ref)
                    if match:
                        col, row = _column_index(match.group(1)), int(match.group(2))
                        applies |= min_col <= col <= max_col and min_row <= row <= max_row
                except ValueError:
                    continue
            if not applies:
                continue
            formula = (validation.findtext(f"{{{MAIN}}}formula1") or "").strip()
            values: set[str] | None = None
            if len(formula) >= 2 and formula[0] == formula[-1] == '"':
                values = set(formula[1:-1].split(","))
            else:
                expression = _resolve_names(formula.lstrip("="), sheet_name, names)
                reference = CELL_REF.fullmatch(expression)
                if reference:
                    target_sheet = (reference.group(1).replace("''", "'") if reference.group(1)
                                    else reference.group(2) or sheet_name).strip()
                    target_part = sheets.get(target_sheet)
                    if target_part:
                        target_root = _xml(parts[target_part][1])
                        first = CELL_COORD.match(reference.group(3))
                        last = CELL_COORD.match(reference.group(4) or reference.group(3))
                        if first and last:
                            c1, c2 = _column_index(first.group(1)), _column_index(last.group(1))
                            r1, r2 = int(first.group(2)), int(last.group(2))
                            if c2 >= c1 and r2 >= r1 and (c2-c1+1)*(r2-r1+1) <= 1024:
                                values = set()
                                cell_map = {node.get("r"): node for node in target_root.xpath(".//m:c", namespaces=NS)}
                                for row in range(r1, r2 + 1):
                                    for col in range(c1, c2 + 1):
                                        ref = f"{get_column_letter(col)}{row}"
                                        item = cell_map.get(ref)
                                        if item is None:
                                            values = None; break
                                        parsed, item_reason = _cell_value(item, shared, rich, dates, epoch)
                                        if parsed is None or item_reason:
                                            values = None; break
                                        values.add(str(parsed.value))
                                    if values is None:
                                        break
            unsupported |= not bool(values)
            if values:
                options = values
        return options, unsupported

    def preflight(self, source: bytes, target: EditTarget, patch: Patch) -> None:
        if hashlib.sha256(source).hexdigest() != patch.source_version_id:
            raise XlsxError("stale_source")
        fresh = next((item for item in self.targets(source) if item.id == patch.target_id), None)
        if fresh is None:
            raise XlsxError("target_not_found")
        if not fresh.editable:
            raise XlsxError(fresh.reason or "not_editable")
        if fresh.fingerprint != patch.expected_fingerprint:
            raise XlsxError("fingerprint_mismatch")
        parts, sheets, shared, rich, dates, epoch, *_ = self._data(source)
        part = sheets[fresh.sheet]
        root = _xml(parts[part][1])
        options, unsupported = self._list_validation_options(
            parts, sheets, shared, rich, dates, epoch, fresh.sheet, fresh.cell, root)
        if unsupported:
            raise XlsxError("unsupported_data_validation")
        if options is not None and str(patch.new_value.value) not in options:
            raise XlsxError("data_validation_value_invalid")

    def unsupported_targets(self, source: bytes) -> list[EditTarget]:
        parts, sheets, shared, rich, dates, epoch, formulas, refs, unknown_formula = self._data(source)
        result: list[EditTarget] = []
        for sheet, part in sheets.items():
            root = _xml(parts[part][1])
            seen: set[str] = set()
            for cell in root.xpath(".//m:c", namespaces=NS):
                value, reason = _cell_value(cell, shared, rich, dates, epoch)
                if value is None and reason in {"formula_cell", "array_formula", "rich_text", "error_cell", "invalid_number", "invalid_date", "invalid_shared_string"}:
                    ref = cell.get("r", "")
                    if cell.find(f"{{{MAIN}}}f") is not None:
                        formula = cell.find(f"{{{MAIN}}}f")
                        if formula.get("t") in {"array", "dataTable"} or (formula.get("ref") and ":" in formula.get("ref", "")):
                            reason = "array_formula"
                    result.append(EditTarget(f"xlsx:{sheet}!{ref}", hashlib.sha256(source).hexdigest(), sheet, ref,
                                             (TypedValue("string", "=" + (cell.find(f"{{{MAIN}}}f").text or ""),
                                                          native=cell.find(f"{{{MAIN}}}f").text or "")
                                              if cell.find(f"{{{MAIN}}}f") is not None
                                              else TypedValue("null", None)),
                                             _fingerprint(cell), cell.get("s"), False, reason,
                                             None if unknown_formula else ()))
                    seen.add(ref)
                formula = cell.find(f"{{{MAIN}}}f")
                if formula is not None and formula.get("t") in {"array", "dataTable"} and formula.get("ref"):
                    try:
                        min_col, min_row, max_col, max_row = range_boundaries(formula.get("ref"))
                        for row in range(min_row, max_row + 1):
                            for col in range(min_col, max_col + 1):
                                ref = f"{get_column_letter(col)}{row}"
                                if ref in seen: continue
                                result.append(EditTarget(f"xlsx:{sheet}!{ref}", hashlib.sha256(source).hexdigest(), sheet, ref,
                                                         TypedValue("null", None), hashlib.sha256(f"array:{sheet}:{ref}".encode()).hexdigest(),
                                                         None, False, "array_formula", None if unknown_formula else ()))
                                seen.add(ref)
                    except ValueError:
                        continue
        return result

    def validate_new_value(self, value: TypedValue) -> None:
        if value.type not in {"string", "integer", "decimal", "date", "datetime", "boolean", "null"}:
            raise XlsxError("invalid_type")
        if value.type == "decimal":
            decimal = Decimal(str(value.value))
            digits = len(decimal.normalize().as_tuple().digits)
            if digits > 15:
                raise XlsxError("excel_numeric_precision_exceeded")
        if value.type == "integer" and len(str(abs(int(value.value)))) > 15:
            raise XlsxError("excel_numeric_precision_exceeded")
        if value.type == "date":
            date.fromisoformat(str(value.value))
        if value.type == "datetime":
            datetime.fromisoformat(str(value.value))

    def unknown_dependencies(self, source: bytes, target: EditTarget) -> bool:
        return target.dependents is None

    def formula_dependents(self, source: bytes, target: EditTarget) -> tuple[tuple[str, ...], bool]:
        parts, sheets, shared, rich, dates, epoch, formulas, refs, unknown_formula = self._data(source)
        deps: set[str] = set()
        for target_sheet, first, last, formula_id in refs:
            if target_sheet != target.sheet:
                continue
            a, b = CELL_COORD.match(first), CELL_COORD.match(last)
            cell = CELL_COORD.match(target.cell)
            if not (a and b and cell):
                continue
            col, row = _column_index(cell.group(1)), int(cell.group(2))
            if (_column_index(a.group(1)) <= col <= _column_index(b.group(1))
                    and int(a.group(2)) <= row <= int(b.group(2))):
                deps.add(formula_id)
        array_unknown = False
        for formula_sheet, formula_cell in formulas:
            formula = formula_cell.find(f"{{{MAIN}}}f")
            if formula.get("t") not in {"array", "dataTable"}:
                continue
            for ref_sheet, first, last, _ in refs:
                if ref_sheet != target.sheet or formula_sheet != target.sheet:
                    continue
                a, b = CELL_COORD.match(first), CELL_COORD.match(last)
                cell = CELL_COORD.match(target.cell)
                if a and b and cell:
                    col, row = _column_index(cell.group(1)), int(cell.group(2))
                    if _column_index(a.group(1)) <= col <= _column_index(b.group(1)) and int(a.group(2)) <= row <= int(b.group(2)):
                        array_unknown = True
        return tuple(sorted(deps)), unknown_formula or array_unknown

    def _encode_value(self, cell: etree._Element, value: TypedValue, epoch) -> None:
        cell.attrib.pop("t", None)
        for child in list(cell): cell.remove(child)
        if value.type == "string":
            cell.set("t", "inlineStr")
            inline = etree.SubElement(cell, f"{{{MAIN}}}is")
            text = etree.SubElement(inline, f"{{{MAIN}}}t")
            text.text = str(value.value)
            if text.text[:1].isspace() or text.text[-1:].isspace():
                text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            return
        if value.type == "boolean":
            cell.set("t", "b")
            raw = "1" if value.value else "0"
        elif value.type in {"date", "datetime"}:
            parsed = date.fromisoformat(str(value.value)) if value.type == "date" else datetime.fromisoformat(str(value.value))
            raw = format(to_excel(parsed, epoch), ".15g")
        elif value.type == "null":
            cell.set("t", "str")
            raw = ""
        elif value.type == "integer":
            raw = str(value.value)
        else:
            raw = format(Decimal(str(value.value)), "f")
        etree.SubElement(cell, f"{{{MAIN}}}v").text = raw

    def permission_for(self, source: bytes, target: EditTarget, value: TypedValue, provenance: dict[str, str]) -> tuple[ChangePermission, ...]:
        parts, sheets, shared, rich, dates, epoch, formulas, refs, unknown_formula = self._data(source)
        root = _xml(parts[sheets[target.sheet]][1])
        cell = root.xpath(f'.//m:c[@r="{target.cell}"]', namespaces=NS)[0]
        new_cell = _xml(etree.tostring(cell))
        self._encode_value(new_cell, value, epoch)
        permissions = [ChangePermission(sheets[target.sheet], target.cell, "replace_value", target.fingerprint, _fingerprint(new_cell))]
        if target.dependents or target.dependents is None:
            permissions.append(ChangePermission("xl/workbook.xml", "calcPr", "set_attribute", None, "fullCalcOnLoad=1"))
        permissions.extend(self._provenance_permissions(parts, provenance))
        return tuple(permissions)

    def _provenance_permissions(self, parts, provenance: dict[str, str]) -> tuple[ChangePermission, ...]:
        serialized = json.dumps(provenance, sort_keys=True, separators=(",", ":"))
        perms: list[ChangePermission] = []
        for part in ("docProps/custom.xml", "_rels/.rels", "[Content_Types].xml"):
            if part not in parts:
                perms.append(ChangePermission(part, PROVENANCE_NAME, "add_part" if part == "docProps/custom.xml" else "set_attribute", None, hashlib.sha256(serialized.encode()).hexdigest()))
            else:
                perms.append(ChangePermission(part, PROVENANCE_NAME, "set_attribute", hashlib.sha256(parts[part][1]).hexdigest(), hashlib.sha256(serialized.encode()).hexdigest()))
        return tuple(perms)

    def apply(self, source: bytes, plan: PatchPlan) -> bytes | None:
        if plan.no_change:
            return None
        if hashlib.sha256(source).hexdigest() != plan.source_sha256:
            raise XlsxError("stale_source")
        parts, sheets, shared, rich, dates, epoch, formulas, refs, unknown_formula = self._data(source)
        patches_by_part: dict[str, list[Patch]] = {}
        for p in plan.patches:
            t = next((x for x in self.targets(source) if x.id == p.target_id), None)
            if t is None: raise XlsxError("target_not_found", p.target_id)
            patches_by_part.setdefault(sheets[t.sheet], []).append(p)
        output_parts = dict(parts)
        for part, patches in patches_by_part.items():
            root = _xml(parts[part][1])
            for patch in patches:
                target = next(t for t in self.targets(source) if t.id == patch.target_id)
                cell = root.xpath(f'.//m:c[@r="{target.cell}"]', namespaces=NS)[0]
                if _fingerprint(cell) != patch.expected_fingerprint: raise XlsxError("fingerprint_mismatch", target.id)
                self._encode_value(cell, patch.new_value, epoch)
            output_parts[part] = (parts[part][0], etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True))
        if plan.dependents or plan.dependencies_unknown:
            wb = _xml(parts["xl/workbook.xml"][1])
            calc = wb.find(f"{{{MAIN}}}calcPr")
            if calc is None: calc = etree.SubElement(wb, f"{{{MAIN}}}calcPr")
            calc.set("fullCalcOnLoad", "1")
            calc.set("forceFullCalc", "1")
            output_parts["xl/workbook.xml"] = (parts["xl/workbook.xml"][0], etree.tostring(wb, xml_declaration=True, encoding="UTF-8", standalone=True))
        embedded_provenance = plan.provenance_dict() | {
            "source_sha256": plan.source_sha256,
            "patch_plan_sha256": plan.plan_sha256,
            "adapter": self.name,
            "adapter_version": self.version,
        }
        self._write_provenance(output_parts, embedded_provenance)
        buf = io.BytesIO()
        stable_timestamp = min(info.date_time for info, _ in parts.values())
        with zipfile.ZipFile(buf, "w") as out:
            for name, (info, data) in parts.items():
                changed = output_parts[name][1]
                out.writestr(info, changed, compress_type=info.compress_type)
            for name in sorted(output_parts.keys() - parts.keys()):
                info = zipfile.ZipInfo(name, date_time=stable_timestamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o600 << 16
                out.writestr(info, output_parts[name][1], compress_type=zipfile.ZIP_DEFLATED)
        return buf.getvalue()

    def _write_provenance(self, parts, provenance: dict[str, str]) -> None:
        value = json.dumps(provenance, sort_keys=True, separators=(",", ":"))
        custom_path = "docProps/custom.xml"
        if custom_path in parts:
            custom = _xml(parts[custom_path][1])
        else:
            custom = etree.Element(f"{{{CUSTOM}}}Properties", nsmap={None: CUSTOM, "vt": VT})
        for prop in custom.xpath(f'./*[local-name()="property" and @name="{PROVENANCE_NAME}"]'):
            custom.remove(prop)
        pids = [int(p.get("pid", "1")) for p in custom if p.get("pid", "").isdigit()]
        prop = etree.SubElement(custom, f"{{{CUSTOM}}}property", fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}", pid=str(max([1, *pids]) + 1), name=PROVENANCE_NAME)
        etree.SubElement(prop, f"{{{VT}}}lpwstr").text = value
        old_info = parts.get(custom_path, (None, b""))[0]
        parts[custom_path] = (old_info, etree.tostring(custom, xml_declaration=True, encoding="UTF-8", standalone=True))
        if "_rels/.rels" in parts:
            rel_info, rel_data = parts["_rels/.rels"]
            rels = _xml(rel_data)
        else:
            rel_info, rels = None, etree.Element(f"{{{PKG_REL}}}Relationships", nsmap={None: PKG_REL})
        if not rels.xpath(f'./p:Relationship[@Type="{CUSTOM_REL}"]', namespaces=NS):
            ids = [int(r.get("Id", "rId0")[3:]) for r in rels if r.get("Id", "").startswith("rId") and r.get("Id")[3:].isdigit()]
            etree.SubElement(rels, f"{{{PKG_REL}}}Relationship", Id=f"rId{max(ids or [0]) + 1}", Type=CUSTOM_REL, Target="docProps/custom.xml")
        parts["_rels/.rels"] = (rel_info, etree.tostring(rels, xml_declaration=True, encoding="UTF-8", standalone=True))
        ct_info, ct_data = parts["[Content_Types].xml"]
        types = _xml(ct_data)
        if not types.xpath('./ct:Override[@PartName="/docProps/custom.xml"]', namespaces=NS):
            etree.SubElement(types, f"{{{CONTENT}}}Override", PartName="/docProps/custom.xml", ContentType=CUSTOM_TYPE)
        parts["[Content_Types].xml"] = (ct_info, etree.tostring(types, xml_declaration=True, encoding="UTF-8", standalone=True))
