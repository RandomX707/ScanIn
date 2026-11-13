"""Single-owner workspace API: document folders, samples and the one-call save flow.

Everything here composes the existing verified engine (ingestion pipeline, restricted parser,
edit-set workflow, independent verifiers); it adds no new trust path.
"""
from __future__ import annotations

import hashlib
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
from packages.api.save_flow import (PdfTextEdit, SaveRejected, SaveRequest, json_value_changes, pdf_edit_candidate, save_native,
                                    save_pdf_text, save_rich, save_text, suffix_of, _guard_ready)
from pydantic import BaseModel, ConfigDict, Field
from packages.core import extraction, pdf_signature
from packages.core.chunking import chunkify
from packages.registry.documents import DocumentRepository, is_download_eligible
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


def _classify_versions(versions: list[dict[str, object]], flagged: set[str]) -> dict[str, str]:
    parent = {str(v["version_id"]): (str(v["parent_version_id"]) if v["parent_version_id"] else None) for v in versions}
    out: dict[str, str] = {}
    for v in versions:
        vid = str(v["version_id"])
        if v["kind"] == "original":
            out[vid] = "original"; continue
        node, seen, cls = vid, set(), "derived"
        while node and node not in seen:
            if node in flagged:
                cls = "unsigned_working_copy"; break
            seen.add(node); node = parent.get(node)
        out[vid] = cls
    return out


def _folder_payload(folder: dict[str, object], flagged: set[str] | None = None) -> dict[str, object]:
    filename = str(folder["filename"])
    versions = list(folder["versions"])  # type: ignore[arg-type]
    numbers = {str(item["version_id"]): index + 1 for index, item in enumerate(versions)}
    classes = _classify_versions(versions, flagged or set())
    return {
        "document_id": str(folder["id"]),
        "name": filename,
        "created_at": folder["created_at"].isoformat(),
        "latest_version_id": str(versions[-1]["version_id"]),
        "demo": filename == DEMO_FILENAME or filename in SAMPLE_FILES.values(),
        # Where an archive-imported document came from (archive name + path inside it); None for direct uploads.
        "source": ({"archive": folder["provenance"].get("archive_name"), "path": folder["provenance"].get("entry_path")}
                   if isinstance(folder.get("provenance"), dict) else None),
        "versions": [{
            "version_id": str(item["version_id"]),
            "number": index + 1,
            "kind": item["kind"],
            "name": version_name(filename, index + 1),
            "created_at": item["created_at"].isoformat(),
            "size_bytes": int(item["size_bytes"]),
            "parent_number": numbers.get(str(item["parent_version_id"])) if item["parent_version_id"] else None,
            "status": "Parsed" if item["kind"] == "original" else "Saved",
            "classification": classes[str(item["version_id"])],
            # VERIFIED / STRUCTURALLY_VERIFIED / INCOMPLETE come from the verifier; text-writer versions are SAVED.
            "outcome": None if item["kind"] == "original" else (item.get("verification_outcome") or "SAVED"),
        } for index, item in enumerate(versions)],
    }


def list_folder_payloads(request: Request, identity: AuthenticatedIdentity) -> list[dict[str, object]]:
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        folders = repo.list_folders()
        flagged = repo.working_copy_version_ids()
    return [_folder_payload(item, flagged) for item in folders]


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
        listed = repo.list_versions(parsed_document)
        order = [str(item["version_id"]) for item in listed]
        if version["kind"] == "derived":
            entry = next((item for item in listed if str(item["version_id"]) == str(parsed_version)), None)
            # Versions released by the verified native workflow follow the repository download policy (VERIFIED, or a
            # structurally verified DOCX). INCOMPLETE ones (e.g. formula results not recalculated) are withheld.
            # Versions written by the text/PDF writers have no workflow manifest and carry their own outcome.
            if entry is not None and entry.get("edit_set_id") is not None and not is_download_eligible(
                    entry.get("verification_outcome"), entry.get("verification_adapter"),
                    {"verification_profile": entry.get("verification_profile"), "structural_checks": entry.get("structural_checks"),
                     "visual_checks": entry.get("visual_checks")}):
                raise APIError(409, "verified_download_required",
                               "This saved version isn't fully verified yet (for example, formula results update when it is opened in Excel), so it can't be downloaded.")
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


DEMO_EDITS = (("within 60 calendar days", "within 45 calendar days", "INR 8,000", "INR 9,000"),
              ("within 45 calendar days", "within 40 calendar days", "INR 9,000", "INR 10,000"))


def _ensure_demo_versions(request: Request, identity: AuthenticatedIdentity, document_id: str) -> None:
    """The demo folder carries v2 and v3 so the version timeline can be explored immediately."""
    parsed = UUID(document_id)
    for index, (old_days, new_days, old_cap, new_cap) in enumerate(DEMO_EDITS, start=2):
        folder = next((f for f in list_folder_payloads(request, identity) if f["document_id"] == document_id), None)
        if folder is None or len(folder["versions"]) >= index:  # type: ignore[arg-type]
            continue
        latest = UUID(str(folder["latest_version_id"]))
        _version, source = _source_version(request, identity, parsed, latest)
        text = source.decode("utf-8").replace(old_days, new_days).replace(old_cap, new_cap)
        save_text(request, identity, parsed, latest, text, ".md")


@router.post("/demo")
def open_demo(request: Request, identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Open the Meridian Group Coverage Agreement sample (created once per tenant) with its v2 and v3."""
    opened = _open_sample(request, identity, DEMO_FILENAME, SAMPLES_DIR / DEMO_FILENAME)
    _ensure_demo_versions(request, identity, str(opened["document_id"]))
    folder = next(f for f in list_folder_payloads(request, identity) if f["document_id"] == opened["document_id"])
    # Like the prototype, the demo opens on the original (v1); v2 and v3 are in the folder strip.
    return {"document_id": opened["document_id"], "version_id": folder["versions"][0]["version_id"],  # type: ignore[index]
            "created": opened["created"]}


@router.post("/documents/{document_id}/versions/{version_id}/save")
def save_version(document_id: str, version_id: str, payload: SaveRequest, request: Request,
                 identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Save the edited values as the next version, in the same format as the original, in one call."""
    try:
        parsed_document, parsed_version = UUID(document_id), UUID(version_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None
    _guard_ready(request, identity, parsed_document, parsed_version)
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        original = DocumentRepository(connection, identity.tenant_id).get_original(parsed_document)
    if original is None:
        raise APIError(404, "not_found", "The requested document was not found.")
    suffix = suffix_of(str(original["filename"]))
    if suffix in {".md", ".markdown", ".html", ".htm"} and payload.html is not None:
        result = save_rich(request, identity, parsed_document, parsed_version, payload.html, suffix)
    elif suffix in extraction.TEXT_EXTENSIONS:
        if payload.text is None:
            raise APIError(422, "text_required", "Nothing changed, so there is no new version to save.")
        result = save_text(request, identity, parsed_document, parsed_version, payload.text, suffix)
    elif suffix == ".json":
        changes = payload.changes or (json_value_changes(request, identity, parsed_document, parsed_version, payload.text)
                                      if payload.text is not None else None)
        if changes:
            result = save_native(request, identity, parsed_document, parsed_version, changes)
        elif payload.text is not None:
            result = save_text(request, identity, parsed_document, parsed_version, payload.text, suffix)
        else:
            raise APIError(422, "patch_set_empty", "Nothing changed, so there is no new version to save.")
    elif suffix == ".pdf" and payload.pdf_edits:
        result = save_pdf_text(request, identity, parsed_document, parsed_version, payload.pdf_edits)
    elif suffix in {".csv", ".xlsx", ".docx", ".pdf"}:
        result = save_native(request, identity, parsed_document, parsed_version, payload.changes)
    else:
        raise APIError(415, "native_adapter_unsupported", "This file type can't be edited.")
    folder = next((f for f in list_folder_payloads(request, identity) if f["document_id"] == document_id), None)
    version = next((v for v in folder["versions"] if v["version_id"] == str(result["version_id"])), None) if folder else None  # type: ignore[union-attr]
    request.app.state.__dict__.get("chunk_text_cache", {}).clear()
    return {**result, "version": version}


def _col_letters(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _index_grid(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID, max_rows: int = 500, max_cols: int = 40):
    """(sheets in the grid shape the section builder uses, styles) for a LARGE workbook, read from its windowed index. The reading map
    looks at the top of each sheet (like the classic preview did); the viewer, editor and comparison see all of it."""
    from packages.api.sheet_index import ensure_index, load_meta, open_sheet
    state, directory, _job = ensure_index(request, identity, document_id, version_id, wait=25.0)
    if state != "ready":
        return None
    meta = load_meta(directory)
    sheets = []
    for item in [x for x in meta["sheets"] if not x.get("hidden")][:6]:
        db = open_sheet(directory, int(item["index"]))
        try:
            found = db.execute("SELECT r,c,v,t,s,e,rs FROM cells WHERE r<=? AND c<=? ORDER BY r,c", (max_rows, max_cols)).fetchall()
        finally:
            db.close()
        width = min(max((c for _, c, *_ in found), default=0), max_cols); height = min(max((r for r, *_ in found), default=0), max_rows)
        grid = [[None] * width for _ in range(height)]
        style_of: dict[str, int] = {}
        for r, c, v, t, st, e, rs in found:
            if st is not None and st >= 0:
                style_of[f"{_col_letters(c)}{r}"] = st
            if t is not None:
                grid[r - 1][c - 1] = {"v": v, "t": t, "e": bool(e), "r": rs, "id": "", "fp": ""}
        sheets.append({"name": item["name"], "rows": grid, "truncated": item["max_row"] > max_rows or item["max_col"] > max_cols,
                       "layout": {"merges": [m for m in item["merges"] if m[0] <= max_rows and m[1] <= max_cols], "cells": style_of, "cols": {}, "rows": {}}})
    return sheets, meta["styles"]


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
    if suffix == ".xlsx" and _is_large_workbook(source):
        grid = _index_grid(request, identity, document_id, version_id)
        if grid is not None:
            targets = [{"sheet": sh["name"], "cell": f"{_col_letters(ci + 1)}{ri + 1}", "value": {"value": cell["v"]}}
                       for sh in grid[0] for ri, row in enumerate(sh["rows"]) for ci, cell in enumerate(row) if cell and cell["v"] != ""]
            text = extraction.xlsx_text(targets, [sh["name"] for sh in grid[0]])
        cache[key] = text
        return text
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


def _link_docx_pages(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID,
                     version: dict[str, object], chunks: list[dict[str, object]]) -> None:
    """Point each DOCX section at the rendered page where its heading/label text appears (best effort, never invented)."""
    import re
    from packages.api.render import render_cache_path, rendered_pdf
    try:
        rendered_pdf(request, identity, document_id, version_id)
        outline = request.app.state.pdf_editor.outline(render_cache_path(request, identity, version_id, str(version["sha256"])))
    except Exception:  # noqa: BLE001 - page links are an enhancement; sections work without them
        return
    norm = lambda t: re.sub(r"\W+", "", t or "").lower()  # noqa: E731
    page_text = [(p["page"], norm(" ".join(line["t"] for line in p["lines"]))) for p in outline.get("pages", [])]
    for chunk in chunks:
        source = chunk.get("source") or {}
        needles = [str(chunk.get("title") or "")] + [str(a.get("label") or "") for a in (source.get("anchors") or [])[:4]] + [str(f.get("label") or "") for f in (chunk.get("facts") or [])[:4]]
        for needle in needles:
            n = norm(needle)
            if len(n) < 4:
                continue
            hit = next((number for number, text in page_text if n in text), None)
            if hit:
                source["pages"] = [hit]; source["label"] = f"Page {hit}" + (f" · {source['label']}" if source.get("label") else "")
                chunk["source"] = source
                break


def _semantic_chunks(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID) -> list[dict[str, object]] | None:
    """Source-linked, rule-based sections (policy PDFs, spreadsheet templates, DOCX forms). None = use the plain chunker."""
    from packages.core import policy_sections, sheet_sections
    from packages.workers.pdf_edit import PdfEditUnavailable
    cache: dict = request.app.state.__dict__.setdefault("semantic_cache", {})
    key = (str(identity.tenant_id), str(version_id))
    if key in cache:
        return cache[key]
    version, source = _source_version(request, identity, document_id, version_id)
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        original = DocumentRepository(connection, identity.tenant_id).get_original(document_id)
    name = str(original["filename"] if original else version["filename"])
    suffix = PurePosixPath(name).suffix.lower()
    path = request.app.state.document_storage.root / str(version["storage_key"])
    chunks: list[dict[str, object]] | None = None
    try:
        if suffix == ".pdf" and source.startswith(b"%PDF-"):
            outline = request.app.state.pdf_editor.outline(path)
            segmented = policy_sections.segment(outline)
            chunks = policy_sections.to_chunks(segmented) if segmented["sections"] else policy_sections.outline_chunks(outline)
        elif suffix == ".xlsx" and _is_large_workbook(source):
            grid = _index_grid(request, identity, document_id, version_id)
            chunks = None
            if grid is not None:
                chunks = []
                for sheet in grid[0]:
                    chunks.extend(sheet_sections.sheet_chunks(sheet, grid[1]))
        elif suffix == ".xlsx":
            report, targets = _all_targets(request, path, name)
            if report.status == "inspected":
                sheets = _grid(targets, list(report.sheets) or ["Sheet1"])
                layout = (report.details or {}).get("layout") or {}
                by_name = {item["name"]: item for item in layout.get("sheets", [])}
                chunks = []
                for sheet in sheets:
                    sheet["layout"] = by_name.get(sheet["name"])
                    chunks.extend(sheet_sections.sheet_chunks(sheet, layout.get("styles", [])))
        elif suffix == ".docx":
            report = request.app.state.restricted_parser.inspect(path, filename=name, include_targets=True)
            chunks = sheet_sections.docx_chunks((report.details or {}).get("blocks", []))
            if chunks and request.app.state.office_renderer.available():
                _link_docx_pages(request, identity, document_id, version_id, version, chunks)
    except (PdfEditUnavailable, ParserNotVerified):
        chunks = None
    except Exception:  # noqa: BLE001 - understanding is best-effort; the plain chunker still serves the document
        chunks = None
    chunks = chunks or None
    if len(cache) > 48:
        cache.clear()
    cache[key] = chunks
    return chunks


@router.get("/documents/{document_id}/versions/{version_id}/chunks")
def version_chunks(document_id: str, version_id: str, request: Request,
                   identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Reading sections of one version: source-linked and rule based (no AI); plain text chunks as the fallback."""
    try:
        parsed_document, parsed_version = UUID(document_id), UUID(version_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None
    text = _version_text(request, identity, parsed_document, parsed_version)
    words = len(text.split()) if text.strip() else 0
    semantic = _semantic_chunks(request, identity, parsed_document, parsed_version) if text.strip() else None
    if semantic:
        sections = [{**{k: item[k] for k in ("id", "title", "summary", "words", "points", "method")},
                     "source": item.get("source"), "facts": item.get("facts", [])} for item in semantic]
    else:
        sections = [{key: item[key] for key in ("id", "title", "summary", "words", "points", "method")} | {"source": None, "facts": []}
                    for item in chunkify(text)]
    return {"sections": sections, "word_count": words, "text_available": bool(sections),
            "excerpt": text[:12000], "excerpt_truncated": len(text) > 12000}


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


def _all_targets_ex(request: Request, path: Path, name: str, pages: int = 10, budget_seconds: float | None = None):
    """(report, targets, incomplete). `incomplete` is True when more target pages exist than were read (page cap or time budget)."""
    import time
    started = time.monotonic()
    parser = request.app.state.restricted_parser
    report = parser.inspect(path, filename=name, include_targets=True)
    targets = list(report.targets)
    page = report.targets_page or {}
    offset = page.get("next_offset")
    while offset is not None and pages > 1:
        if budget_seconds is not None and time.monotonic() - started > budget_seconds:
            break
        more = parser.inspect(path, filename=name, include_targets=True, target_offset=int(offset))
        targets.extend(more.targets)
        offset = (more.targets_page or {}).get("next_offset")
        pages -= 1
    return report, targets, offset is not None


def _all_targets(request: Request, path: Path, name: str, pages: int = 10):
    report, targets, _incomplete = _all_targets_ex(request, path, name, pages)
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
        by_sheet.setdefault(str(target["sheet"]) or sheets[0], {})[(row, column)] = {
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


def _is_large_workbook(source: bytes) -> bool:
    """Worksheet XML (uncompressed, from the zip directory only: nothing is decompressed) big enough that paging every cell is too slow."""
    import zipfile
    from packages.api.sheet_index import VIRTUAL_XML_BYTES
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as z:
            return sum(i.file_size for i in z.infolist() if i.filename.startswith("xl/worksheets/")) > VIRTUAL_XML_BYTES
    except zipfile.BadZipFile:
        return False


def _virtual_xlsx_preview(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID, done) -> dict[str, object]:
    """Windowed workbook: metadata only here; the browser asks for cell windows. While the index is being built, say so (never partial)."""
    from packages.api.sheet_index import ensure_index, load_meta
    state, directory, job = ensure_index(request, identity, document_id, version_id, wait=1.5)
    if state != "ready":
        failed = state in ("failed", "cancelled")
        return done("xlsx", "none", "This large workbook couldn't be prepared for viewing." if failed else "Preparing this large workbook…",
                    sheets=[], styles=[], virtual={"state": state, "error": job.error if job else None})
    meta = load_meta(directory)
    capability = meta["capability"]
    editable = capability["level"] == "editable"
    reasons = list(capability.get("reasons") or [])
    return done("xlsx", "grid" if editable else "none", None if editable else (cell_reason(reasons[0]) if reasons else file_reason(reasons, ".xlsx")),
                sheets=[], styles=[], virtual={"state": "ready", "total_cells": meta["total_cells"], "sheets": [{"name": x["name"], "max_row": x["max_row"], "max_col": x["max_col"]} for x in meta["sheets"]]})


@router.get("/documents/{document_id}/versions/{version_id}/preview")
def version_preview(document_id: str, version_id: str, request: Request, virtual: bool = Query(default=False),
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
        return {**model, "format": format_, "edit": {"mode": mode, "reason": reason}, **extra}

    try:
        if suffix in extraction.TEXT_EXTENSIONS or (suffix == ".json" and not source.startswith(b"PK")):
            if len(source) > extraction.MAX_TEXT_BYTES or b"\x00" in source[:8192]:
                return done("text", "none", "This file is too large to edit here.")
            text, encoding = extraction.decode_text(source)
            kind = {".md": "md", ".markdown": "md", ".html": "html", ".htm": "html", ".xml": "xml", ".json": "json"}.get(suffix, "txt")
            from packages.core import richtext
            rendered = (richtext.render_markdown(text) if kind == "md"
                        else richtext.render_html_document(text) if kind == "html" else None)
            return done("text", "text", None, text=text, encoding=encoding, kind=kind, html=rendered)
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
            return done("image", "none", file_reason([], suffix))
        if suffix == ".pptx":
            report = request.app.state.restricted_parser.inspect(path, filename=name)
            slides = (report.details or {}).get("slides", [])
            return done("pptx", "none", file_reason([], suffix), slides=slides)
        if suffix == ".xlsx" and (virtual or _is_large_workbook(source)):
            return _virtual_xlsx_preview(request, identity, parsed_document, parsed_version, done)
        if suffix in {".csv", ".xlsx"}:
            # A very large sheet costs seconds per page of targets: read what fits in the budget and say so (partial=True).
            report, targets, partial = _all_targets_ex(request, path, name, pages=10, budget_seconds=25.0)
            if report.status != "inspected":
                return done("csv" if suffix == ".csv" else "xlsx", "none", file_reason(report.capability.get("reasons", []), suffix))
            level = str(report.capability.get("level"))
            reasons = list(report.capability.get("reasons") or [])
            sheets = list(report.sheets) or ["Sheet1"]
            grid = _grid(targets, sheets)
            layout = (report.details or {}).get("layout") or {}
            by_name = {item["name"]: item for item in layout.get("sheets", [])}
            for item in grid:
                if item["name"] in by_name:
                    item["layout"] = by_name[item["name"]]
            editable = level == "editable"
            return done("csv" if suffix == ".csv" else "xlsx", "grid" if editable else "none",
                        None if editable else (cell_reason(reasons[0]) if reasons else file_reason(reasons, suffix)),
                        sheets=grid, styles=layout.get("styles", []), partial=partial)
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
            renderer = request.app.state.office_renderer
            from packages.api.render import FIDELITY_NOTE
            render = {"available": renderer.available(), "engine": "libreoffice-sandboxed", "note": FIDELITY_NOTE,
                      "reason": renderer.reason_unavailable()}
            return done("docx", "docx" if level == "editable" else "none",
                        None if level == "editable" else file_reason(reasons, suffix), blocks=blocks, render=render)
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
            signature = pdf_signature.classify(source)
            from packages.api.compare import lineage_classes, load_family
            from packages.api.working_copy import ACKNOWLEDGEMENT, signature_payload, validate_signatures
            validation = validate_signatures(request, source, hashlib.sha256(source).hexdigest())
            versions_, events_, _meta = load_family(request, identity, parsed_document)
            classification = lineage_classes(versions_, events_).get(str(parsed_version), "derived")
            text_editable = False
            if editable and signature["status"] in pdf_signature.REASONS:
                editable = False                       # a signed form must not be filled in place
            if editable:
                reason = None
            elif signature["status"] in pdf_signature.REASONS:
                reason = pdf_signature.REASONS[signature["status"]]
            elif details.get("xfa"):
                reason = FILE_REASONS["xfa"]
            elif (level == "editable" or set(reasons) <= {"no_acroform_fields"}) and any(not p["scanned"] for p in pages):
                text_editable = True                   # ordinary selectable-text PDF: edit the text itself
                reason = None
            elif level == "editable":
                reason = FILE_REASONS["scanned_or_image_pdf"]
            elif pages and all(p["scanned"] for p in pages):
                reason = FILE_REASONS["scanned_or_image_pdf"]
            else:
                reason = file_reason(reasons, suffix)
            return done("pdf", "pdf" if editable else "pdf_text" if text_editable else "none", reason, pages=pages,
                        fields=fields, text_available=any(not p["scanned"] for p in pages),
                        signature=signature_payload(validation), classification=classification,
                        working_copy={"available": signature["status"] != "none" and classification != "unsigned_working_copy",
                                      "acknowledgement": ACKNOWLEDGEMENT})
    except ParserNotVerified:
        raise APIError(503, "view_not_verified", "Safe document viewing is unavailable.") from None
    return done("none", "none", file_reason([], suffix))


@router.get("/documents/{document_id}/versions/{version_id}/pdf-text")
def pdf_text_runs(document_id: str, version_id: str, request: Request, page: int = Query(ge=1, le=5000),
                  identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Selectable text runs (with page coordinates) of one page of an ordinary PDF version."""
    from packages.workers.pdf_edit import PdfEditUnavailable
    try:
        parsed_document, parsed_version = UUID(document_id), UUID(version_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None
    version, source = _source_version(request, identity, parsed_document, parsed_version)
    if not source.startswith(b"%PDF-"):
        raise APIError(415, "not_a_pdf", "This version isn't a PDF.")
    status = pdf_signature.classify(source)["status"]
    path = request.app.state.document_storage.root / str(version["storage_key"])
    try:
        data = request.app.state.pdf_editor.runs(path, page)
    except PdfEditUnavailable:
        raise APIError(503, "pdf_edit_unavailable", "PDF text isn't available right now. Try again in a moment.") from None
    locked = pdf_signature.REASONS.get(status)
    if locked:
        for run in data["runs"]:
            run["e"], run["r"] = False, locked
    else:
        from packages.api.save_flow import plain
        for run in data["runs"]:
            if not run["e"]:
                run["r"] = plain(str(run["r"]))
    return data


class PdfPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pdf_edits: list[PdfTextEdit] = Field(min_length=1, max_length=200)


@router.post("/documents/{document_id}/versions/{version_id}/pdf-edit/preview")
def pdf_edit_preview(document_id: str, version_id: str, payload: PdfPreviewRequest, request: Request,
                     identity: AuthenticatedIdentity = Depends(require_identity)) -> Response:
    """The edited PDF exactly as it would be saved (not stored). Refusals explain themselves."""
    try:
        parsed_document, parsed_version = UUID(document_id), UUID(version_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None
    _guard_ready(request, identity, parsed_document, parsed_version)
    data, report, _version = pdf_edit_candidate(request, identity, parsed_document, parsed_version, payload.pdf_edits)
    return Response(content=data, media_type="application/pdf", headers={
        "Cache-Control": "no-store", "X-Preview-Outcome": str(report["outcome"]),
        "X-Preview-Checks": str(len(report["checks"])),
        "X-Preview-Substituted": str(sum(1 for e in report["edits"] if e.get("substitute_font"))),
        "Access-Control-Expose-Headers": "X-Preview-Outcome, X-Preview-Checks, X-Preview-Substituted"})
