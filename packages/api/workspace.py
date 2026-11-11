"""Single-owner workspace API: document folders, samples and the one-call save flow.

Everything here composes the existing verified engine (ingestion pipeline, restricted parser,
edit-set workflow, independent verifiers); it adds no new trust path.
"""
from __future__ import annotations

import io
from pathlib import Path, PurePosixPath
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from urllib.parse import quote

from packages.api.auth import AuthenticatedIdentity, require_identity
from packages.api.db_context import api_tenant_transaction
from packages.api.documents import _filename, _source_version, ingest_stream, inspect_document
from packages.api.errors import APIError
from packages.core import extraction
from packages.core.chunking import chunkify
from packages.registry.documents import DocumentRepository
from packages.workers.parser import ParserNotVerified
from packages.storage.local import ImmutableLocalStorage, StoredFile

router = APIRouter(prefix="/api", tags=["workspace"])

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
# Display order of the format playground; each maps to one synthetic file served by the backend.
SAMPLE_FILES: dict[str, str] = {
    "pdf": "Provider_Network_Agreement.pdf",
    "docx": "Employee_Onboarding_Guide.docx",
    "xlsx": "Member_Enrollment_Register.xlsx",
    "pptx": "Quarterly_Service_Review.pptx",
    "png": "Claim_Receipt.png",
    "txt": "Service_Desk_Notes.txt",
    "md": "Project_Change_Log.md",
    "csv": "Monthly_Claims_Summary.csv",
    "json": "Members_API_Response.json",
    "html": "Welcome_Packet.html",
    "xml": "Eligibility_Exchange.xml",
}
DEMO_FILENAME = "Meridian · Group Coverage Agreement.md"


def split_name(filename: str) -> tuple[str, str]:
    path = PurePosixPath(filename)
    suffix = path.suffix
    return (filename[: -len(suffix)] if suffix else filename), suffix


def version_name(filename: str, number: int) -> str:
    """Display name of a version: the original file name for v1, then ``<stem> · vN<ext>``."""
    stem, suffix = split_name(filename)
    return filename if number == 1 else f"{stem} · v{number}{suffix}"


def download_name(filename: str, number: int) -> str:
    stem, suffix = split_name(filename)
    return filename if number == 1 else f"{stem}_v{number}{suffix}"


def _folder_payload(folder: dict[str, object]) -> dict[str, object]:
    filename = str(folder["filename"])
    versions = list(folder["versions"])  # type: ignore[arg-type]
    numbers = {str(item["version_id"]): index + 1 for index, item in enumerate(versions)}
    return {
        "document_id": str(folder["id"]),
        "name": filename,
        "created_at": folder["created_at"].isoformat(),
        "latest_version_id": str(versions[-1]["version_id"]),
        "demo": filename == DEMO_FILENAME or filename in SAMPLE_FILES.values(),
        "versions": [{
            "version_id": str(item["version_id"]),
            "number": index + 1,
            "kind": item["kind"],
            "name": version_name(filename, index + 1),
            "created_at": item["created_at"].isoformat(),
            "size_bytes": int(item["size_bytes"]),
            "parent_number": numbers.get(str(item["parent_version_id"])) if item["parent_version_id"] else None,
            "status": "Parsed" if item["kind"] == "original" else "Saved",
        } for index, item in enumerate(versions)],
    }


def list_folder_payloads(request: Request, identity: AuthenticatedIdentity) -> list[dict[str, object]]:
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        folders = DocumentRepository(connection, identity.tenant_id).list_folders()
    return [_folder_payload(item) for item in folders]


@router.get("/folders")
def list_folders(request: Request, q: str = Query(default="", max_length=200),
                 identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Originals with their version families; ``q`` matches the original or any version name."""
    items = list_folder_payloads(request, identity)
    needle = q.strip().lower()
    if needle:
        items = [item for item in items
                 if needle in " ".join([str(item["name"])] + [str(v["name"]) for v in item["versions"]]).lower()]  # type: ignore[union-attr]
    return {"items": items}


@router.get("/documents/{document_id}/versions/{version_id}/file")
def download_version_file(document_id: str, version_id: str, request: Request,
                          identity: AuthenticatedIdentity = Depends(require_identity)) -> Response:
    """Exact stored bytes of one version: the untouched original or a saved same-format version."""
    try:
        parsed_document, parsed_version = UUID(document_id), UUID(version_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document version was not found.") from None
    storage: ImmutableLocalStorage = request.app.state.document_storage
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        version = repo.get_version(parsed_document, parsed_version)
        if version is None:
            raise APIError(404, "not_found", "The requested document version was not found.")
        order = [str(item["version_id"]) for item in repo.list_versions(parsed_document)]
        stored = StoredFile(identity.tenant_id, parsed_document, storage.root / str(version["storage_key"]),
                            str(version["storage_key"]), int(version["size_bytes"]), str(version["sha256"]))
        content = storage.read_original(stored)
        number = order.index(str(parsed_version)) + 1 if str(parsed_version) in order else 1
        original = repo.get_original(parsed_document)
        filename = _filename(str(original["filename"] if original else version["filename"]))
        media_type = str(version.get("mime") or "application/octet-stream")
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(download_name(filename, number), safe='')}",
                             "X-Content-Type-Options": "nosniff"})


@router.get("/samples")
def list_samples(identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    return {"items": [{"key": key, "name": name} for key, name in SAMPLE_FILES.items()]}


def _open_sample(request: Request, identity: AuthenticatedIdentity, filename: str, path: Path) -> dict[str, object]:
    """Open the tenant's existing copy of a synthetic sample or ingest it through the normal safe path."""
    for item in list_folder_payloads(request, identity):
        if item["name"] == filename:
            return {"document_id": item["document_id"], "version_id": item["latest_version_id"], "created": False}
    data = path.read_bytes()
    registered = ingest_stream(request, identity, _filename(filename), io.BytesIO(data))
    inspect_document(str(registered["document_id"]), request, False, identity)
    return {"document_id": registered["document_id"], "version_id": registered["version_id"], "created": True}


@router.post("/samples/{key}")
def open_sample(key: str, request: Request,
                identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    name = SAMPLE_FILES.get(key)
    if name is None:
        raise APIError(404, "not_found", "That sample is not available.")
    return _open_sample(request, identity, name, SAMPLES_DIR / name)


@router.post("/demo")
def open_demo(request: Request, identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Open the Meridian Group Coverage Agreement sample (created once per tenant)."""
    return _open_sample(request, identity, DEMO_FILENAME, SAMPLES_DIR / DEMO_FILENAME)


def _version_text(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID) -> str:
    """Readable text of one immutable version (empty when it has none, e.g. scanned PDFs and images)."""
    cache: dict = request.app.state.__dict__.setdefault("chunk_text_cache", {})
    key = (str(identity.tenant_id), str(version_id))
    if key in cache:
        return cache[key]
    version, source = _source_version(request, identity, document_id, version_id)
    original = None
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        original = DocumentRepository(connection, identity.tenant_id).get_original(document_id)
    name = str(original["filename"] if original else version["filename"])
    suffix = PurePosixPath(name).suffix.lower()
    text = ""
    if suffix in {".pdf", ".docx", ".xlsx", ".pptx"} and source.startswith((b"%PDF-", b"PK\x03\x04")):
        try:
            report = request.app.state.restricted_parser.inspect(
                request.app.state.document_storage.root / str(version["storage_key"]),
                filename=name, include_targets=suffix in {".docx", ".xlsx"})
        except ParserNotVerified:
            raise APIError(503, "view_not_verified", "Safe document viewing is unavailable.") from None
        details = report.details or {}
        if suffix == ".pdf":
            text = extraction.pdf_text(details)
        elif suffix == ".docx":
            text = extraction.docx_text(details)
        elif suffix == ".xlsx":
            text = extraction.xlsx_text(list(report.targets), list(report.sheets))
        else:
            text = extraction.pptx_text(details)
    elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} or len(source) > extraction.MAX_TEXT_BYTES:
        text = ""
    elif b"\x00" not in source[:8192]:
        decoded, _encoding = extraction.decode_text(source)
        if suffix == ".csv":
            text = extraction.csv_text(name, decoded)
        elif suffix == ".json":
            text = extraction.json_text(decoded)
        elif suffix in {".html", ".htm"}:
            text = extraction.html_to_text(decoded)
        else:
            text = decoded
    if len(cache) > 64:
        cache.clear()
    cache[key] = text
    return text


@router.get("/documents/{document_id}/versions/{version_id}/chunks")
def version_chunks(document_id: str, version_id: str, request: Request,
                   identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Deterministic reading sections and key takeaways of one version (rule based, no AI)."""
    try:
        parsed_document, parsed_version = UUID(document_id), UUID(version_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None
    text = _version_text(request, identity, parsed_document, parsed_version)
    sections = chunkify(text)
    words = len(text.split()) if text.strip() else 0
    return {"sections": [{key: item[key] for key in ("id", "title", "summary", "words", "points", "method")}
                         for item in sections],
            "word_count": words, "text_available": bool(sections)}


# ---------------------------------------------------------------------------------------------------------
# Preview / Edits model
# ---------------------------------------------------------------------------------------------------------

CELL_REASONS = {
    "formula_cell": "Formula cell: it recalculates from other cells.",
    "array_formula": "Array formula: it can't be changed here.",
    "protected_cell_locked": "Protected cell: locked by the workbook.",
    "rich_text": "Rich-text cell: editing would lose its formatting.",
    "unsupported_data_validation": "Restricted by a validation rule.",
    "data_validations": "Restricted by a validation rule.",
    "time_value_read_only": "Time values can't be changed here.",
    "error_cell": "Error value: it can't be changed here.",
    "unsupported_dialect": "This file layout can't be edited safely.",
    "mixed_line_endings": "This file mixes line endings, so it stays read-only.",
}
DOCX_REASONS = {
    "multi_run_span": "Text with mixed formatting is read-only.",
    "content_control_or_textbox": "Text boxes and content controls are read-only.",
    "tracked_change_region": "Text inside tracked changes is read-only.",
    "field_code": "Field codes are read-only.",
    "tracked_changes_present": "This document has tracked changes, so it stays read-only.",
    "document_protected": "This document is protected, so it stays read-only.",
    "macros_present": "This document contains macros, so it stays read-only.",
    "has_macros": "This document contains macros, so it stays read-only.",
}
FILE_REASONS = {
    "signed_pdf": "This PDF is signed, so editing would break its signature.",
    "certified_pdf": "This PDF is certified, so editing would break its certification.",
    "encrypted_pdf": "This PDF is encrypted, so it can only be read.",
    "scanned_or_image_pdf": "This PDF is a scan with no text layer, so it can't be edited.",
    "xfa_form": "This PDF uses an XFA form, which can't be edited safely.",
    "xfa": "This PDF uses an XFA form, which can't be edited safely.",
    "no_acroform_fields": "This PDF has no form fields, so there is nothing to change in place.",
    "tracked_changes_present": "This document has tracked changes, so it stays read-only.",
    "document_protected": "This document is protected, so it stays read-only.",
    "macros_present": "This file contains macros, so it stays read-only.",
    "encrypted_or_legacy_office": "This file is encrypted or in a legacy format, so it can only be read.",
    "legacy_office_unsupported": "This legacy Office format can only be read.",
    "external_links_present": "This workbook has external links, so it stays read-only.",
    "adapter_not_implemented": "This file type can be read here but not edited.",
}


def cell_reason(code: str | None) -> str:
    return CELL_REASONS.get(str(code or "").split(":")[-1], "Locked: this value can't be changed safely.")


def file_reason(codes: list[str] | tuple[str, ...], suffix: str) -> str:
    for code in codes:
        if code in FILE_REASONS:
            return FILE_REASONS[code]
    if suffix == ".pptx":
        return "PowerPoint files can be read here but not edited."
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        return "Images can be viewed here but not edited."
    if suffix == ".pdf":
        return "This PDF can't be edited without risking its layout."
    return "This file can't be edited without risking its layout."


def _all_targets(request: Request, path: Path, name: str, pages: int = 10):
    parser = request.app.state.restricted_parser
    report = parser.inspect(path, filename=name, include_targets=True)
    targets = list(report.targets)
    page = report.targets_page or {}
    offset = page.get("next_offset")
    while offset is not None and pages > 1:
        more = parser.inspect(path, filename=name, include_targets=True, target_offset=int(offset))
        targets.extend(more.targets)
        offset = (more.targets_page or {}).get("next_offset")
        pages -= 1
    return report, targets


MAX_GRID_ROWS = 500
MAX_GRID_COLS = 40


def _grid(targets: list[dict[str, object]], sheets: list[str]) -> list[dict[str, object]]:
    by_sheet: dict[str, dict[tuple[int, int], dict[str, object]]] = {name: {} for name in sheets}
    for target in targets:
        row, column = extraction._column_number(str(target["cell"]))
        if row < 1 or column < 1:
            continue
        value = (target.get("value") or {})
        raw = value.get("value")
        by_sheet.setdefault(str(target["sheet"]), {})[(row, column)] = {
            "v": "" if raw is None else (str(raw).lower() if isinstance(raw, bool) else str(raw)),
            "t": value.get("type"), "id": target["id"], "fp": target["fingerprint"],
            "e": bool(target.get("editable")),
            "r": None if target.get("editable") else cell_reason(str(target.get("reason") or "")),
        }
    result = []
    for name, cells in by_sheet.items():
        if not cells:
            result.append({"name": name, "rows": [], "truncated": False})
            continue
        max_row = max(row for row, _ in cells)
        max_col = max(column for _, column in cells)
        rows = [[cells.get((row, column)) for column in range(1, min(max_col, MAX_GRID_COLS) + 1)]
                for row in range(1, min(max_row, MAX_GRID_ROWS) + 1)]
        result.append({"name": name, "rows": rows, "truncated": max_row > MAX_GRID_ROWS or max_col > MAX_GRID_COLS})
    return result


@router.get("/documents/{document_id}/versions/{version_id}/preview")
def version_preview(document_id: str, version_id: str, request: Request,
                    identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Render model of one version: Original-tab previews plus what the Edits tab may edit in place."""
    try:
        parsed_document, parsed_version = UUID(document_id), UUID(version_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None
    version, source = _source_version(request, identity, parsed_document, parsed_version)
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        original = repo.get_original(parsed_document)
        document = repo.get(parsed_document)
    name = str(original["filename"] if original else version["filename"])
    suffix = PurePosixPath(name).suffix.lower()
    is_latest = bool(document and str(document["current_version_id"]) == str(parsed_version))
    path = request.app.state.document_storage.root / str(version["storage_key"])
    model: dict[str, object] = {"name": name, "is_latest": is_latest, "size_bytes": len(source)}

    def done(format_: str, mode: str, reason: str | None = None, **extra: object) -> dict[str, object]:
        if mode != "none" and not is_latest:
            mode, reason = "none", "Open the latest version to make changes."
        return {**model, "format": format_, "edit": {"mode": mode, "reason": reason}, **extra}

    try:
        if suffix in extraction.TEXT_EXTENSIONS or (suffix == ".json" and not source.startswith(b"PK")):
            if len(source) > extraction.MAX_TEXT_BYTES or b"\x00" in source[:8192]:
                return done("text", "none", "This file is too large to edit here.")
            text, encoding = extraction.decode_text(source)
            kind = {".md": "md", ".markdown": "md", ".html": "html", ".htm": "html", ".xml": "xml", ".json": "json"}.get(suffix, "txt")
            return done("text", "text", None, text=text, encoding=encoding, kind=kind)
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
            return done("image", "none", file_reason([], suffix))
        if suffix == ".pptx":
            report = request.app.state.restricted_parser.inspect(path, filename=name)
            slides = (report.details or {}).get("slides", [])
            return done("pptx", "none", file_reason([], suffix), slides=slides)
        if suffix in {".csv", ".xlsx"}:
            report, targets = _all_targets(request, path, name)
            if report.status != "inspected":
                return done("csv" if suffix == ".csv" else "xlsx", "none", file_reason(report.capability.get("reasons", []), suffix))
            level = str(report.capability.get("level"))
            reasons = list(report.capability.get("reasons") or [])
            sheets = list(report.sheets) or ["Sheet1"]
            grid = _grid(targets, sheets)
            editable = level == "editable"
            return done("csv" if suffix == ".csv" else "xlsx", "grid" if editable else "none",
                        None if editable else (cell_reason(reasons[0]) if reasons else file_reason(reasons, suffix)),
                        sheets=grid)
        if suffix == ".docx":
            report = request.app.state.restricted_parser.inspect(path, filename=name, include_targets=True)
            details = report.details or {}
            targets = {item["id"]: item for item in details.get("targets", [])}
            blocks = []
            for block in details.get("blocks", []):
                ids = [t for t in block.get("target_ids", []) if t in targets]
                single = targets[ids[0]] if len(ids) == 1 else None
                editable = bool(single and single.get("editable"))
                reason = None
                if not editable:
                    code = str(single.get("reason") if single else ("multi_run_span" if len(ids) > 1 else "")).strip()
                    reason = DOCX_REASONS.get(code, "This text is read-only.")
                blocks.append({"id": block["id"], "kind": block["kind"], "text": block["text"],
                               "heading": block.get("heading_level"), "table": block.get("table"),
                               "target_id": single["id"] if editable and single else None,
                               "fp": single["fingerprint"] if editable and single else None,
                               "e": editable, "r": reason})
            level = str(report.capability.get("level"))
            reasons = list(report.capability.get("reasons") or [])
            return done("docx", "docx" if level == "editable" else "none",
                        None if level == "editable" else file_reason(reasons, suffix), blocks=blocks)
        if suffix == ".pdf":
            report = request.app.state.restricted_parser.inspect(path, filename=name, include_targets=True)
            details = report.details or {}
            level = str(report.capability.get("level"))
            reasons = list(report.capability.get("reasons") or [])
            pages = [{"page": p["page"], "width": p["width"], "height": p["height"],
                      "scanned": bool(p.get("scanned_or_image_only"))} for p in details.get("pages", [])]
            fields = [{"id": f["id"], "name": f["name"], "type": f["type"], "value": (f.get("value") or {}).get("value"),
                       "options": f.get("options") or [], "max_length": f.get("max_length"), "fp": f["fingerprint"],
                       "e": bool(f.get("editable")), "r": None if f.get("editable") else cell_reason(f.get("reason")),
                       "widgets": (f.get("native_locator") or {}).get("widgets", [])} for f in details.get("fields", [])]
            editable = level == "editable" and bool(fields)
            if editable:
                reason = None
            elif details.get("xfa"):
                reason = FILE_REASONS["xfa"]
            elif level == "editable":
                reason = FILE_REASONS["no_acroform_fields"]
            elif pages and all(p["scanned"] for p in pages):
                reason = FILE_REASONS["scanned_or_image_pdf"]
            else:
                reason = file_reason(reasons, suffix)
            return done("pdf", "pdf" if editable else "none", reason, pages=pages, fields=fields,
                        text_available=any(not p["scanned"] for p in pages))
    except ParserNotVerified:
        raise APIError(503, "view_not_verified", "Safe document viewing is unavailable.") from None
    return done("none", "none", file_reason([], suffix))
