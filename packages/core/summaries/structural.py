from __future__ import annotations

import json
import re
from typing import Any, Protocol


class SummaryProvider(Protocol):
    """Extension point for content-grounded summary providers."""

    def summarize(self, *, source: bytes, adapter: str | None, details: dict[str, Any] | None,
                  targets: list[dict[str, Any]] | None = None, sheets: list[str] | None = None) -> dict[str, Any]: ...


class AIExplanationProvider(Protocol):
    """Future explicit-request provider; it must never determine authorization."""

    def explain(self, *, structural_summary: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]: ...


_INSURANCE_FIELDS = (
    ("claim number", re.compile(r"claim.?number|claim.?id|claim.?reference", re.I)),
    ("policy number", re.compile(r"policy.?number|policy.?id|policy.?reference", re.I)),
    ("policyholder", re.compile(r"policy.?holder|insured.?name", re.I)),
    ("claimant", re.compile(r"claimant", re.I)),
    ("coverage", re.compile(r"coverage|benefit", re.I)),
    ("loss date", re.compile(r"loss.?date|incident.?date", re.I)),
    ("insurer", re.compile(r"insurer|carrier", re.I)),
    ("premium", re.compile(r"premium", re.I)),
)


class StructuralSummaryProvider:
    """Create a bounded, deterministic summary without retaining document values."""

    def summarize(self, *, source: bytes, adapter: str | None, details: dict[str, Any] | None,
                  targets: list[dict[str, Any]] | None = None, sheets: list[str] | None = None) -> dict[str, Any]:
        try:
            return self._summarize(source=source, adapter=adapter, details=details, targets=targets, sheets=sheets)
        except (ValueError, UnicodeError, KeyError, TypeError, OSError) as error:
            # Inspection can identify a read-only/partially supported file whose format parser
            # cannot provide structured facts. Keep that honest and never turn it into a denial.
            import hashlib
            return {"source_sha256": hashlib.sha256(source).hexdigest(), "format": adapter or "unknown",
                    "apparent_document_type": None,
                    "description": f"This {adapter or 'file'} was identified, but its structure could not be read reliably. Its purpose is unclear.",
                    "statistics": {}, "observed_fields": [],
                    "limitations": ["The safe format parser could not extract enough reliable structure for a summary."],
                    "generation_method": "structural", "evidence_references": []}

    def _summarize(self, *, source: bytes, adapter: str | None, details: dict[str, Any] | None,
                   targets: list[dict[str, Any]] | None = None, sheets: list[str] | None = None) -> dict[str, Any]:
        targets = targets or []
        details = details or {}
        observed: list[dict[str, str]] = []
        evidence: list[dict[str, str]] = []
        limitations: list[str] = []
        stats: dict[str, Any] = {}
        apparent_type: str | None = None

        if adapter == "csv":
            # Use the adapter's full parser (not the bounded target response) for correct delimited record counts.
            from packages.core.adapters.csv_adapter.adapter import CsvAdapter
            document = CsvAdapter()._parse(source)
            nonempty_rows = sum(1 for row in range(1, len(document.row_lengths) + 1)
                                if any(cell.row == row and cell.value.strip() for cell in document.cells))
            headers = [document.cell(1, col).value.strip() for col in range(1, max(document.row_lengths, default=0) + 1)]
            has_header = bool(headers and any(not _looks_data_like(value) for value in headers))
            record_count = max(0, nonempty_rows - (1 if has_header else 0))
            stats = {"record_count": record_count, "column_count": len([name for name in headers if name])}
            names = headers if has_header else [f"Column {index}" for index in range(1, max(document.row_lengths, default=0) + 1)]
            for index, name in enumerate(names, start=1):
                if not name:
                    continue
                observed.append({"name": name[:100], "kind": "column"})
                evidence.append({"reference": f"csv:column:{index}", "label": name[:100]})
            apparent_type = _insurance_type(headers if has_header else [])
            fields = ", ".join(names[:8]) or "unnamed columns"
            description = f"This CSV contains {record_count} data records across {stats['column_count']} columns. Its column headings include {fields}."
            if apparent_type:
                description += f" The field names are consistent with an {apparent_type.replace(' document', '')} dataset."
            else:
                description += " The document purpose is unclear from its column headings alone."
        elif adapter == "json":
            from packages.core.adapters.json_adapter.adapter import JsonAdapter
            value = JsonAdapter().parse(source)
            root_kind = "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "scalar"
            top_keys = list(value.keys()) if isinstance(value, dict) else []
            nested_descriptions: list[str] = []
            classification_names = [str(key) for key in top_keys]
            stats = {"top_level_structure": root_kind, "top_level_key_count": len(top_keys),
                     "top_level_item_count": len(value) if isinstance(value, (dict, list)) else 1}
            for key in top_keys[:12]:
                kind = _json_kind(value[key])
                observed.append({"name": str(key)[:100], "kind": kind})
                evidence.append({"reference": f"json:/{_pointer(str(key))}", "label": str(key)[:100]})
                nested = value[key] if isinstance(value[key], dict) else (
                    next((item for item in value[key] if isinstance(item, dict)), None) if isinstance(value[key], list) else None)
                if isinstance(nested, dict):
                    child_names = [str(child) for child in list(nested.keys())[:8]]
                    classification_names.extend(child_names)
                    nested_descriptions.append(f"{key} contains " + ", ".join(child_names))
                    for child in child_names:
                        observed.append({"name": f"{key}.{child}"[:100], "kind": "nested field"})
                        evidence.append({"reference": f"json:/{_pointer(str(key))}/{_pointer(child)}", "label": f"{key}.{child}"[:100]})
            apparent_type = _insurance_type(classification_names)
            shape = ", ".join(f"{key} ({_json_kind(value[key])})" for key in top_keys[:8]) or "no named top-level fields"
            description = f"This JSON file has a top-level {root_kind} structure with {stats['top_level_key_count']} named fields. The structure includes {shape}."
            if nested_descriptions:
                description += " Nested content includes " + "; ".join(nested_descriptions[:5]) + "."
            if apparent_type:
                description += f" Its field names are consistent with an {apparent_type}."
            else:
                description += " Its business purpose is unclear from the available field names."
            if len(top_keys) > 12:
                limitations.append("Only the first 12 top-level field names are listed; the source remains unchanged.")
        elif adapter == "xlsx":
            from packages.core.adapters.xlsx.adapter import XlsxAdapter
            parts, workbook_sheets, shared, rich, dates, epoch, formulas, refs, unknown = XlsxAdapter()._data(source)
            sheet_stats = []
            for sheet_name, part in workbook_sheets.items():
                import packages.core.adapters.xlsx.adapter as xlsx_module
                root = xlsx_module._xml(parts[part][1])
                refs_in_sheet = [node.get("r", "") for node in root.xpath(".//m:c", namespaces=xlsx_module.NS)]
                max_row = max((int(re.search(r"\d+$", ref).group()) for ref in refs_in_sheet if re.search(r"\d+$", ref)), default=0)
                max_col = max((xlsx_module._column_index(re.match(r"[A-Z]+", ref).group()) for ref in refs_in_sheet if re.match(r"[A-Z]+", ref)), default=0)
                headers = []
                for target in targets:
                    if target.get("editable") and target.get("sheet") == sheet_name and re.fullmatch(r"[A-Z]+1", str(target.get("cell", ""))):
                        label = target.get("value", {}).get("value")
                        if isinstance(label, str) and label:
                            headers.append(label)
                sheet_stats.append({"name": sheet_name, "rows": max_row, "columns": max_col, "headers": headers[:8]})
                observed.append({"name": sheet_name, "kind": "worksheet"})
                evidence.append({"reference": f"xlsx:{sheet_name}", "label": sheet_name})
            stats = {"worksheet_count": len(sheet_stats), "worksheets": sheet_stats}
            names = [header for sheet in sheet_stats for header in sheet["headers"]]
            apparent_type = _insurance_type(names)
            summary_sheets = "; ".join(f"{sheet['name']} ({sheet['rows']} rows × {sheet['columns']} columns)" for sheet in sheet_stats[:6]) or "no readable worksheets"
            description = f"This workbook contains {len(sheet_stats)} worksheets: {summary_sheets}."
            if names:
                description += " The first-row headings indicate tabular content, including " + ", ".join(names[:8]) + "."
            else:
                description += " No reliable first-row headings were identified."
            if apparent_type:
                description += f" The field names are consistent with an {apparent_type}."
            elif not names:
                description += " Its purpose is unclear from the available structure."
        elif adapter == "pdf":
            pages = details.get("pages", [])
            form_fields = details.get("fields", []) or [
                {"id": item.get("id"), "name": item.get("label"), "type": item.get("field_type")}
                for item in targets if str(item.get("id", "")).startswith("pdf:")]
            readable = [page for page in pages if not page.get("scanned_or_image_only")]
            text = " ".join(str(page.get("text", "")) for page in readable)
            labels = [str(item.get("name", "")) for item in form_fields if item.get("name")]
            observed.extend({"name": label[:100], "kind": "AcroForm field"} for label in labels[:12])
            evidence.extend({"reference": f"pdf:field:{item.get('id')}", "label": str(item.get("name", ""))[:100]}
                            for item in form_fields[:12])
            stats = {"page_count": len(pages), "readable_text_pages": len(readable),
                     "scanned_or_image_only_pages": len(pages) - len(readable), "acroform_field_count": len(form_fields)}
            apparent_type = _text_insurance_type(text + " " + " ".join(labels))
            page_word = "page" if len(pages) == 1 else "pages"
            readable_word = "page" if len(readable) == 1 else "pages"
            scanned_count = len(pages) - len(readable)
            scanned_word = "page" if scanned_count == 1 else "pages"
            description = f"This PDF has {len(pages)} {page_word}, including {len(readable)} {readable_word} with extractable text and {scanned_count} scanned or image-only {scanned_word}."
            if labels:
                description += " It contains AcroForm fields labeled " + ", ".join(labels[:6]) + "."
            if apparent_type:
                description += f" Readable text and field labels are consistent with an {apparent_type}."
            elif not text.strip() and not labels:
                description += " No readable text or AcroForm fields were found, so its purpose is unclear."
            elif not apparent_type and not labels:
                description += " Text is present, but its purpose is unclear from the available structural evidence."
            if len(readable) < len(pages):
                limitations.append("Scanned/image-only pages are displayed without OCR; their text was not summarized.")
        elif adapter == "docx":
            blocks = details.get("blocks", [])
            by_kind: dict[str, int] = {}
            for block in blocks:
                kind = str(block.get("kind", "paragraph"))
                by_kind[kind] = by_kind.get(kind, 0) + 1
                text = str(block.get("text", "")).strip()
                for label in _recognized_sections(text):
                    if not any(item["name"] == label for item in observed):
                        observed.append({"name": label, "kind": "recognized section"})
                        evidence.append({"reference": str(block.get("id", "docx")), "label": label})
            text = " ".join(str(block.get("text", "")) for block in blocks)
            apparent_type = _text_insurance_type(text)
            stats = {"paragraph_and_content_block_count": len(blocks), "sections_by_kind": by_kind}
            sections = "; ".join(f"{name.replace('_', ' ')}: {count}" for name, count in sorted(by_kind.items())) or "no readable text blocks"
            description = f"This Word document contains {sections}."
            if apparent_type:
                description += f" The extracted text is consistent with an {apparent_type}."
            elif not text.strip():
                description += " Its purpose is unclear because no readable text was extracted."
            else:
                description += " Its purpose is unclear from the available structural evidence."
            if details.get("preview_note"):
                limitations.append(str(details["preview_note"]))
        else:
            adapter = adapter or "unknown"
            stats = {"size_bytes": len(source)}
            limitations.append("This format has no supported structural summary provider.")
            description = "The file was identified, but its structure could not be read reliably for summary. Its purpose is unclear."

        description = _limit_words(description, 100)
        return {"source_sha256": __import__("hashlib").sha256(source).hexdigest(), "format": adapter or "unknown",
                "apparent_document_type": apparent_type, "description": description, "statistics": stats,
                "observed_fields": observed[:20], "limitations": limitations,
                "generation_method": "structural", "evidence_references": evidence[:30]}


def _insurance_type(names: list[str]) -> str | None:
    found = {label for label, pattern in _INSURANCE_FIELDS if any(pattern.search(str(name)) for name in names)}
    if "claim number" in found and len(found & {"policy number", "policyholder", "claimant", "coverage", "loss date"}) >= 1:
        return "insurance claim document"
    if "policy number" in found and len(found & {"policyholder", "coverage", "premium", "insurer"}) >= 1:
        return "insurance policy document"
    return None


def _text_insurance_type(text: str) -> str | None:
    normalized = text.lower()
    if "insurance" in normalized and "claim" in normalized and any(word in normalized for word in ("policyholder", "claimant", "loss date")):
        return "insurance claim document"
    if "insurance" in normalized and "policy" in normalized and any(word in normalized for word in ("coverage", "premium", "policyholder")):
        return "insurance policy document"
    keys = [label for label, pattern in _INSURANCE_FIELDS if pattern.search(text)]
    return _insurance_type(keys)


def _looks_data_like(value: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:-?\d+(?:\.\d+)?|\d{4}-\d{2}-\d{2}|true|false|null)\s*", value, re.I))


def _json_kind(value: Any) -> str:
    if isinstance(value, dict): return "object"
    if isinstance(value, list): return "array"
    if value is None: return "null"
    if isinstance(value, bool): return "boolean"
    if isinstance(value, (int, float)): return "number"
    return "text"


def _pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _recognized_sections(text: str) -> list[str]:
    labels = []
    for label, pattern in _INSURANCE_FIELDS:
        if pattern.search(text):
            labels.append(label.title())
    for label, pattern in (("Coverage details", r"\bcoverage\b"), ("Claim information", r"\bclaim\b"),
                           ("Policy information", r"\bpolicy\b"), ("Premium information", r"\bpremium\b")):
        if pattern and re.search(pattern, text, re.I) and label not in labels:
            labels.append(label)
    return labels


def _limit_words(value: str, limit: int) -> str:
    words = value.split()
    return " ".join(words[:limit])
