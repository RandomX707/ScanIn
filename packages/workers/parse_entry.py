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
class ParserRejected(ValueError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def inspect_bytes(data: bytes, *, filename: str = "", max_expansion_ratio: int = 100,
                  include_targets: bool = False) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    if not data or len(data) > MAX_FILE_BYTES:
        raise ParserRejected("file_size_limit")
    registry = AdapterRegistry()
    selection = registry.inspect(data, filename=filename, max_expansion_ratio=max_expansion_ratio)
    profile = selection.profile
    if profile.level == "rejected":
        reason = profile.reasons[0] if profile.reasons else "invalid_document"
        raise ParserRejected(reason)
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
    if include_targets and supported:
        safe_targets = [target for target in source_adapter.targets(data) if target.editable]
        rejected_targets = source_adapter.unsupported_targets(data)
        all_targets = safe_targets + rejected_targets
        result["targets"] = [_target_dict(target) for target in all_targets[:MAX_TARGETS]]
        result["targets_truncated"] = len(all_targets) > MAX_TARGETS
    if supported and profile.adapter == "pdf":
        result["details"] = _pdf_details(data, registry.pdf, include_targets=include_targets)
    elif supported and profile.adapter == "docx":
        result["details"] = registry.docx.details(data, include_targets=include_targets)
    from packages.core.summaries.structural import StructuralSummaryProvider
    summary_targets = all_targets if include_targets and supported else (
        source_adapter.targets(data) if supported else [])
    result["structural_summary"] = StructuralSummaryProvider().summarize(
        source=data, adapter=profile.adapter, details=result.get("details"), targets=[_target_dict(item) for item in summary_targets],
        sheets=list(profile.sheets))
    return result


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
            spans: list[dict[str, Any]] = []
            for char_index, char in enumerate(text[:2000]):
                if char.isspace():
                    continue
                try:
                    left, bottom, right, top = text_page.get_charbox(char_index)
                except (IndexError, ValueError):
                    continue
                spans.append({"start": char_index, "end": char_index + 1, "text": char,
                              "bbox": [round(left, 2), round(bottom, 2), round(right, 2), round(top, 2)]})
            pages.append({"page": page_number, "width": round(page.get_size()[0], 2),
                          "height": round(page.get_size()[1], 2), "text": text,
                          "text_spans": spans, "scanned_or_image_only": not bool(text.strip())})
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
    args = parser.parse_args()
    try:
        path = Path(args.input)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ParserRejected("file_size_limit")
        result = inspect_bytes(path.read_bytes(), filename=args.filename or path.name, include_targets=args.targets)
    except ParserRejected as error:
        result = {"status": "rejected", "reason": error.reason_code}
    except (OSError, ValueError):
        result = {"status": "rejected", "reason": "parser_input_unavailable"}
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
