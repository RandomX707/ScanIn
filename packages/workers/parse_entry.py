from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

from packages.core.adapters.registry import AdapterRegistry


MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TARGETS = 2000
MAX_TARGET_LOOKUP_IDS = 2000
MAX_TARGET_ID_LENGTH = 512


class ParserRejected(ValueError):
    def __init__(self, reason_code: str, selection=None, digest: str | None = None, size_bytes: int | None = None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.selection = selection
        self.digest = digest
        self.size_bytes = size_bytes


def _bounded_target_ids(target_ids: Any) -> list[str] | None:
    if target_ids is None:
        return None
    if (not isinstance(target_ids, list) or len(target_ids) > MAX_TARGET_LOOKUP_IDS
            or any(not isinstance(item, str) or not item or len(item) > MAX_TARGET_ID_LENGTH for item in target_ids)):
        raise ParserRejected("target_lookup_invalid")
    return list(dict.fromkeys(target_ids))


def inspect_bytes(data: bytes, *, filename: str = "", max_expansion_ratio: int = 100,
                  include_targets: bool = False, include_summary_evidence: bool = False,
                  target_offset: int = 0, target_ids: list[str] | None = None) -> dict[str, Any]:
    """Inspect one source. Targets are returned as one bounded page (``target_offset``) or, when
    ``target_ids`` is given, as an exact lookup of at most MAX_TARGET_LOOKUP_IDS identifiers."""
    if not isinstance(target_offset, int) or isinstance(target_offset, bool) or target_offset < 0:
        raise ParserRejected("target_page_invalid")
    target_ids = _bounded_target_ids(target_ids)
    digest = hashlib.sha256(data).hexdigest()
    if not data or len(data) > MAX_FILE_BYTES:
        raise ParserRejected("file_size_limit")
    registry = AdapterRegistry()
    selection = registry.inspect(data, filename=filename, max_expansion_ratio=max_expansion_ratio)
    profile = selection.profile
    if profile.level == "rejected":
        reason = profile.reasons[0] if profile.reasons else "invalid_document"
        raise ParserRejected(reason, selection, digest, len(data))
    supported = profile.adapter in {"xlsx", "csv", "json", "pdf", "docx"}
    source_adapter = ({"xlsx": registry.xlsx, "csv": registry.csv, "json": registry.json,
                       "pdf": registry.pdf, "docx": registry.docx}.get(profile.adapter) if supported else None)
    result = {
        "status": "inspected" if supported else "identified",
        "sha256": digest,
        "size_bytes": len(data),
        "media_type": selection.media_type,
        "capability": {"level": "Read-only" if profile.level == "read_only" else profile.level,
                        "reasons": list(profile.reasons)},
        "adapter": profile.adapter,
        "adapter_version": profile.adapter_version,
        "sheets": list(profile.sheets),
        "features": list(profile.features),
        "findings": list(selection.findings),
        "score": selection.score,
    }
    # A CSV that is read-only because its bytes cannot be parsed (for example undecodable text) has no
    # targets. Report that honestly instead of failing the whole inspection; an editable profile whose
    # document cannot be parsed is still an error (fail closed).
    structure_available = supported
    if supported and profile.adapter == "csv":
        try:
            source_adapter.document(data)
        except ValueError:
            if profile.level == "editable":
                raise
            structure_available = False
    # Adapters with a bounded target API (CSV) never build the full target list: the page or the exact
    # lookup is produced from their compact index, parsed once for this request.
    windowed = structure_available and callable(getattr(source_adapter, "target_window", None))
    all_targets: list[Any] = []
    if include_targets and supported and not structure_available:
        result["targets"] = []
        result["targets_page"] = {"mode": "lookup" if target_ids is not None else "page", "offset": target_offset,
                                  "limit": MAX_TARGETS, "total": 0, "next_offset": None,
                                  **({"requested": len(target_ids), "found": 0} if target_ids is not None else {})}
        result["targets_truncated"] = False
    elif include_targets and supported:
        fast_selected = None
        if target_ids is not None and not windowed and callable(getattr(source_adapter, "targets_by_id_fast", None)):
            # Exact lookup of a few cells: build only those targets (the same per-cell logic as the full scan). Any id that is not an
            # ordinary value cell falls through to the full scan below, so unsupported/read-only cells are reported exactly as before.
            found = source_adapter.targets_by_id_fast(data, target_ids)
            if all(i in found for i in target_ids):
                fast_selected = [found[i] for i in target_ids]
        if fast_selected is not None:
            total = len(fast_selected)
        elif windowed:
            total = source_adapter.target_count(data)
        else:
            # Every parser-discovered value stays viewable (and eligible for sidecar corrections) with
            # its real editable flag and reason. Order is unchanged for existing documents: editable
            # targets, then unsupported targets, then read-only values that only targets() reports
            # (for example locked cells on protected sheets or unsupported list validation), each id
            # exactly once. Adapters never emit targets for cells without a parsed value.
            adapter_targets = source_adapter.targets(data)
            ordered = ([target for target in adapter_targets if target.editable]
                       + list(source_adapter.unsupported_targets(data))
                       + [target for target in adapter_targets if not target.editable])
            seen_ids: set[str] = set()
            all_targets = []
            for target in ordered:
                if target.id not in seen_ids:
                    seen_ids.add(target.id)
                    all_targets.append(target)
            total = len(all_targets)
        if target_ids is not None:
            wanted = set(target_ids)
            selected = (fast_selected if fast_selected is not None else source_adapter.targets_by_id(data, target_ids) if windowed
                        else [target for target in all_targets if target.id in wanted])
            result["targets"] = [_target_dict(target) for target in selected]
            result["targets_page"] = {"mode": "lookup", "offset": 0, "limit": MAX_TARGET_LOOKUP_IDS,
                                      "total": total, "requested": len(target_ids),
                                      "found": len(result["targets"]), "next_offset": None}
            result["targets_truncated"] = False
        else:
            page = (source_adapter.target_window(data, target_offset, MAX_TARGETS) if windowed
                    else all_targets[target_offset:target_offset + MAX_TARGETS])
            end = target_offset + len(page)
            result["targets"] = [_target_dict(target) for target in page]
            result["targets_page"] = {"mode": "page", "offset": target_offset, "limit": MAX_TARGETS,
                                      "total": total, "next_offset": end if end < total else None}
            # True when this response does not carry every target (another page exists or precedes it).
            result["targets_truncated"] = target_offset > 0 or end < total
    if supported and profile.adapter == "pdf":
        result["details"] = _pdf_details(data, registry.pdf, include_targets=include_targets)
    elif supported and profile.adapter == "docx":
        result["details"] = registry.docx.details(data, include_targets=include_targets)
    elif supported and profile.adapter == "xlsx" and include_targets and not target_offset:
        from packages.core.xlsx_layout import extract as _xlsx_layout
        try:
            result["details"] = {"layout": _xlsx_layout(data)}
        except Exception:  # noqa: BLE001 - layout is cosmetic; the editable grid still works without it
            result["details"] = {"layout": None}
    elif profile.adapter == "pptx":
        # Read-only slide text for the document map; parsed here, inside the restricted worker.
        from packages.core.extraction import pptx_details
        try:
            result["details"] = pptx_details(data)
        except Exception:  # noqa: BLE001 - an unreadable deck is simply shown without text
            result["details"] = {"slides": []}
    from packages.core.summaries.structural import StructuralSummaryProvider
    _apply_document_level_editability(result, profile)
    if include_targets and supported:
        # Document-wide count of natively editable targets (independent of the page or lookup returned).
        if profile.level != "editable" or not structure_available:
            editable_total = 0
        elif windowed:
            editable_total = 0 if source_adapter.document(data).mixed_line_endings else total
        else:
            editable_total = sum(1 for target in all_targets if target.editable)
        result["targets_editable_total"] = editable_total
    if profile.adapter == "csv":
        # The CSV summary is computed from the parsed document, never from target dictionaries.
        summary_targets: list[Any] = []
        parsed = source_adapter.document(data) if structure_available else None
    else:
        summary_targets = all_targets if include_targets and supported else (
            source_adapter.targets(data) if supported else [])
        parsed = None
    result["structural_summary"] = StructuralSummaryProvider().summarize(
        source=data, adapter=profile.adapter, details=result.get("details"), targets=[_target_dict(item) for item in summary_targets],
        sheets=list(profile.sheets), parsed=parsed)
    if include_summary_evidence and structure_available:
        result["summary_evidence"] = _summary_evidence(data, profile.adapter, result.get("details"), source_adapter)
    return result


def _apply_document_level_editability(result: dict[str, Any], profile: Any) -> None:
    """A document that is not natively editable has no natively editable targets.

    Targets keep their values (they remain viewable and eligible for sidecar data corrections), but
    every target, PDF field and DOCX target reports editable=false with the document-level reason."""
    if profile.level == "editable":
        return
    reason = profile.reasons[0] if profile.reasons else "document_not_editable"
    for target in result.get("targets", []) or []:
        if target.get("editable"):
            target["editable"], target["reason"] = False, reason
    details = result.get("details") or {}
    for key in ("fields", "targets"):
        for item in details.get(key, []) or []:
            if isinstance(item, dict) and item.get("editable"):
                item["editable"], item["reason"] = False, reason


def _summary_evidence(data: bytes, adapter: str, details: dict[str, Any] | None, source_adapter: Any) -> list[dict[str, str]]:
    """Build a small, location-addressable content bundle only for an explicit AI request."""
    items: list[dict[str, str]] = []
    if adapter == "csv":
        document = source_adapter._parse(data)
        max_col = max(document.row_lengths, default=0)
        headers = [(cell.value.strip()[:400] if cell else "") for col in range(1, max_col + 1)
                   for cell in (document.cell(1, col),)]
        if headers:
            items.append({"id": "csv:header", "location": "CSV header", "text": " | ".join(headers)})
            for row in range(2, min(len(document.row_lengths), 8) + 1):
                cells = [(cell.value.strip()[:400] if cell else "") for col in range(1, max_col + 1)
                         for cell in (document.cell(row, col),)]
                items.append({"id": f"csv:row:{row}", "location": f"CSV record {row - 1}",
                              "text": "; ".join(f"{headers[col - 1] or f'Column {col}'}: {value}"
                                                 for col, value in enumerate(cells, 1) if value)})
    elif adapter == "json":
        import json as json_module
        value = json_module.loads(data)
        stack: list[tuple[str, Any, int]] = [("$", value, 0)]
        while stack and len(items) < 40:
            path, node, depth = stack.pop()
            if depth > 12:
                continue
            if isinstance(node, dict):
                for key, child in reversed(list(node.items())):
                    escaped = str(key).replace("~", "~0").replace("/", "~1")[:120]
                    stack.append((f"{path}/{escaped}", child, depth + 1))
            elif isinstance(node, list):
                for index, child in reversed(list(enumerate(node[:8]))):
                    stack.append((f"{path}/{index}", child, depth + 1))
            else:
                raw_value = node[:400] if isinstance(node, str) else node
                serialized = json_module.dumps(raw_value, ensure_ascii=False)[:420]
                items.append({"id": f"json:{path}", "location": f"JSON {path}"[:180],
                              "text": f"{path[:180]}: {serialized}"})
    elif adapter == "xlsx":
        targets = source_adapter.targets(data)
        for target in targets[:40]:
            value = target.value.canonical().get("value")
            if value is not None and str(value).strip():
                items.append({"id": target.id, "location": f"Worksheet {target.sheet}, cell {target.cell}"[:180],
                              "text": f"{target.sheet}!{target.cell}: {str(value)[:400]}"})
    elif adapter == "pdf":
        details = details or {}
        for page in details.get("pages", [])[:20]:
            text = str(page.get("text", "")).strip()
            if page.get("scanned_or_image_only"):
                items.append({"id": f"pdf:scan-limitation:page:{page['page']}",
                              "location": f"PDF page {page['page']} inspection limitation",
                              "text": f"Page {page['page']} has no readable extracted text. No OCR was performed; document content on this page cannot be reliably explained."})
            elif text:
                items.append({"id": f"pdf:page:{page['page']}", "location": f"PDF page {page['page']}", "text": text[:500]})
        for field in details.get("fields", [])[:40]:
            value = field.get("value")
            items.append({"id": str(field["id"]), "location": f"PDF form field {field['name']}",
                          "text": f"{field['name']}: {str(value)[:350]}"})
        if not items and details.get("page_count"):
            items.append({"id": "pdf:scan-limitation", "location": "PDF inspection limitation",
                          "text": f"{details['page_count']} page(s) contain no readable extracted text. No OCR was performed."})
    elif adapter == "docx":
        blocks = (details or {}).get("blocks", [])
        selected_ids: set[str] = set()
        selected_blocks: list[dict[str, Any]] = []
        for kind in ("header", "footer", "table_cell", "paragraph"):
            preferred = next((block for block in blocks if block.get("kind") == kind and
                              (kind != "paragraph" or not block.get("heading_level"))), None)
            if preferred and preferred["id"] not in selected_ids:
                selected_ids.add(preferred["id"])
                selected_blocks.append(preferred)
        selected_blocks.extend(block for block in blocks if block.get("heading_level") and block["id"] not in selected_ids)
        selected_ids.update(block["id"] for block in selected_blocks)
        selected_blocks.extend(block for block in blocks if block["id"] not in selected_ids)
        for block in selected_blocks[:40]:
            text = str(block.get("text", "")).strip()
            if text:
                location = (f"DOCX heading level {block['heading_level']} {block['id']}" if block.get("heading_level")
                            else f"DOCX {block['kind']} {block['id']}")
                items.append({"id": str(block["id"]), "location": location[:180], "text": text[:500]})
    bounded: list[dict[str, str]] = []
    remaining = 8000
    for item in items:
        if remaining <= 0:
            break
        text = item["text"][:min(500, remaining)]
        if text:
            bounded.append({**item, "text": text})
            remaining -= len(text)
    return bounded


def _target_dict(target: Any) -> dict[str, Any]:
    result = {
        "id": target.id,
        "sheet": target.sheet,
        "cell": target.cell,
        "label": target.label,
        "value": target.value.canonical(),
        "fingerprint": target.fingerprint,
        "editable": target.editable,
        "reason": target.reason,
        "dependents": list(target.dependents) if target.dependents is not None else None,
    }
    for name in ("field_type", "native_locator", "options", "max_length", "font_size", "part"):
        value = getattr(target, name, None)
        if value is not None:
            result[name] = list(value) if name == "options" else value
    return result


def _pdf_details(data: bytes, adapter, *, include_targets: bool) -> dict[str, Any]:
    from pypdf import PdfReader
    import pypdfium2 as pdfium

    reader = PdfReader(io.BytesIO(data), strict=True)
    fields = adapter.targets(data) if include_targets else []
    field_rows = [{"id": field.id, "name": field.label, "type": field.field_type,
                   "value": field.value.canonical(), "editable": field.editable, "reason": field.reason,
                   "fingerprint": field.fingerprint, "native_locator": field.native_locator,
                   "options": list(field.options), "max_length": field.max_length}
                  for field in fields]
    pages: list[dict[str, Any]] = []
    document = pdfium.PdfDocument(data)
    try:
        for page_number, page in enumerate(document, start=1):
            text_page = page.get_textpage()
            count = min(text_page.count_chars(), 10000)
            text = text_page.get_text_range(0, count)
            pages.append({"page": page_number, "width": round(page.get_size()[0], 2),
                          "height": round(page.get_size()[1], 2), "text": text,
                          "scanned_or_image_only": not bool(text.strip())})
    finally:
        document.close()
    return {"page_count": len(reader.pages), "title": str((reader.metadata or {}).get("/Title", "")),
            "author": str((reader.metadata or {}).get("/Author", "")), "pages": pages,
            "fields": field_rows, "xfa": bool(adapter._xfa_present(data)) if hasattr(adapter, "_xfa_present") else False,
            "preview_note": "Scanned pages are shown without OCR; only detected AcroForm fields are editable."}


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input", required=True)
    parser.add_argument("--filename", default="")
    parser.add_argument("--targets", action="store_true")
    parser.add_argument("--summary-evidence", action="store_true")
    parser.add_argument("--target-offset", type=int, default=0)
    parser.add_argument("--target-ids", default=None, help="JSON list of exact target identifiers")
    args = parser.parse_args()
    try:
        path = Path(args.input)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ParserRejected("file_size_limit")
        target_ids = json.loads(args.target_ids) if args.target_ids is not None else None
        result = inspect_bytes(path.read_bytes(), filename=args.filename or path.name,
                               include_targets=args.targets, include_summary_evidence=args.summary_evidence,
                               target_offset=args.target_offset, target_ids=target_ids)
    except ParserRejected as error:
        selection = error.selection
        result = {"status": "rejected", "reason": error.reason_code,
                  "sha256": error.digest, "size_bytes": error.size_bytes,
                  "media_type": selection.media_type if selection else "application/octet-stream",
                  "capability": {"level": "rejected", "reasons": [error.reason_code]},
                  "adapter": selection.profile.adapter if selection else None,
                  "adapter_version": selection.profile.adapter_version if selection else None,
                  "sheets": [], "features": [],
                  "findings": list(selection.findings) if selection else [],
                  "score": selection.score if selection else 0.0}
    except (OSError, ValueError):
        result = {"status": "rejected", "reason": "parser_input_unavailable"}
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
