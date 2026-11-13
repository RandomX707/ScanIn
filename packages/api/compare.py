"""Version comparison API: compares PERSISTED version files (via sandboxed workers), never replayed edit events.

Read-only: no endpoint here can create, modify or delete a document, version or storage object.
"""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

import logging
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from packages.api.auth import AuthenticatedIdentity, require_identity
from packages.api.db_context import api_tenant_transaction
from packages.api.documents import _source_version
from packages.api.errors import APIError
from packages.core import compare as engine, extraction, pdf_signature
from packages.registry.documents import DocumentRepository
from packages.workers.parser import ParserNotVerified
from packages.workers.pdf_edit import PdfEditUnavailable

router = APIRouter(prefix="/api", tags=["compare"])
TEXT_SUFFIXES = set(extraction.TEXT_EXTENSIONS) | {".json", ".csv"}


def lineage_classes(versions: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, str]:
    """Read authoritative persisted lineage; legacy rows without metadata stay visibly unknown."""
    return {str(v["version_id"]): ("original" if v["kind"] == "original" else str(v.get("lineage_class") or "unknown"))
            for v in versions}


def _ancestors(parent: dict[str, str | None], node: str) -> list[str]:
    chain, seen = [], set()
    while node and node not in seen:
        chain.append(node)
        seen.add(node)
        node = parent.get(node)  # type: ignore[assignment]
    return chain


def relation(parent: dict[str, str | None], base: str, target: str) -> dict[str, Any]:
    if base == target:
        return {"type": "same", "path": [base]}
    up_t, up_b = _ancestors(parent, target), _ancestors(parent, base)
    if base in up_t:
        return {"type": "base_is_ancestor", "path": list(reversed(up_t[: up_t.index(base) + 1]))}
    if target in up_b:
        return {"type": "target_is_ancestor", "path": up_b[: up_b.index(target) + 1]}
    common = next((n for n in up_b if n in set(up_t)), None)
    if common is None:
        return {"type": "unrelated", "path": []}
    # route from the base UP to the common ancestor, then DOWN to the target
    return {"type": "branch", "common_ancestor": common,
            "path": up_b[: up_b.index(common) + 1] + list(reversed(up_t[: up_t.index(common)]))}


# ---------------------------------------------------------------- snapshots (worker-backed)
def _snapshot_pdf(request: Request, path: Path) -> dict[str, Any]:
    return request.app.state.pdf_editor.outline(path)


def _snapshot_docx(request: Request, path: Path, name: str) -> dict[str, Any]:
    report = request.app.state.restricted_parser.inspect(path, filename=name)
    details = report.details or {}
    if report.status != "inspected":
        raise APIError(422, "compare_unreadable", "This version couldn't be read safely for comparison.")
    return {"blocks": [{k: b.get(k) for k in ("id", "kind", "text", "table", "fmt", "props")} for b in details.get("blocks", [])],
            "media": details.get("media", [])}


def _snapshot_text(source: bytes) -> dict[str, Any]:
    text, _ = extraction.decode_text(source)
    return {"lines": [{"t": line} for line in text.splitlines()[:50000]]}


def diff_lines(base: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    changes = []
    lb, lt = base["lines"], target["lines"]
    for tag, i, j in engine._align([x["t"] for x in lb], [x["t"] for x in lt]):
        if tag == "same":
            continue
        n = (j if j is not None else i) + 1
        changes.append({"kind": {"mod": "modified", "del": "removed", "add": "added"}[tag], "category": "text",
                        "location": {"line": n}, "label": f"Line {n}", "before": lb[i]["t"] if i is not None else None,
                        "after": lt[j]["t"] if j is not None else None})
    return engine._finish(changes, ["Compared line by line."])


def snapshot(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version: dict[str, Any], source: bytes, name: str) -> tuple[str, dict[str, Any]]:
    suffix = PurePosixPath(name).suffix.lower()
    cache: dict = request.app.state.__dict__.setdefault("compare_snapshots", {})
    key = (str(identity.tenant_id), str(version["version_id"]), str(version["sha256"]))
    if key in cache:
        return cache[key]
    path = request.app.state.document_storage.root / str(version["storage_key"])
    try:
        if suffix == ".pdf":
            result = ("pdf", _snapshot_pdf(request, path))
        elif suffix == ".docx":
            result = ("docx", _snapshot_docx(request, path, name))
        elif suffix in TEXT_SUFFIXES:
            result = ("text", _snapshot_text(source))
        else:
            raise APIError(415, "compare_unsupported", "Comparing this file type isn't supported yet.")
    except (ParserNotVerified, PdfEditUnavailable):
        raise APIError(503, "compare_unavailable", "Comparison is unavailable right now. Try again in a moment.") from None
    if len(cache) > 40:
        cache.clear()
    cache[key] = result
    return result


# ---------------------------------------------------------------- endpoints
def _version_card(number: int, item: dict[str, Any], classes: dict[str, str], events_by_version: dict[str, dict[str, Any]],
                  parent_numbers: dict[str, int]) -> dict[str, Any]:
    vid = str(item["version_id"])
    event = events_by_version.get(vid)
    payload = (event or {}).get("payload") or {}
    outcome = None if item["kind"] == "original" else (item.get("verification_outcome") or payload.get("outcome") or "SAVED")
    return {"version_id": vid, "number": number, "kind": item["kind"], "classification": classes.get(vid, "unknown"),
            "parent_number": parent_numbers.get(str(item["parent_version_id"])) if item["parent_version_id"] else None,
            "created_at": item["created_at"].isoformat(), "size_bytes": int(item["size_bytes"]), "sha256": str(item["sha256"]),
            "outcome": outcome, "download_available": bool(item.get("download_available")),
            # Recorded facts only: never inferred. None when the audit trail has no actor for this version.
            "actor": (event or {}).get("actor_id"), "recorded_at": (event["ts"].isoformat() if event and event.get("ts") else None),
            "modification": payload.get("writer") or ("native_release" if item.get("edit_set_id") else None),
            "edit_count": payload.get("edit_count")}


def load_family(request: Request, identity: AuthenticatedIdentity, document_id: UUID) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        document = repo.get(document_id)
        if document is None:
            raise APIError(404, "not_found", "The requested document was not found.")
        versions = repo.list_versions(document_id)
        events = repo.list_document_audit(document_id, limit=500)
        original = repo.get_original(document_id)
    return versions, events, {"filename": str(original["filename"] if original else document["filename"])}


def _page(result: dict[str, Any], offset: int, limit: int, kind: str | None = None, category: str | None = None) -> dict[str, Any]:
    """A page of the (optionally filtered) change list. `summary` always describes the WHOLE comparison, never just the page."""
    changes = result["changes"]
    categories: dict[str, int] = {}
    for c in changes:
        categories[c["category"]] = categories.get(c["category"], 0) + 1
    if kind or category:
        changes = [c for c in changes if (not kind or c["kind"] == kind) and (not category or c["category"] == category)]
    return {**result, "changes": changes[offset:offset + limit], "categories": categories, "page": {"offset": offset, "limit": limit, "total": len(changes)}}


def _xlsx_comparison(request: Request, identity: AuthenticatedIdentity, doc: UUID, vb: UUID, vt: UUID, rows: dict[str, dict[str, Any]], wait: float):
    """COMPLETE comparison of two workbook versions from their windowed indexes, or a `building` state (never a partial result)."""
    import time
    from packages.api.sheet_index import ensure_index, load_meta, open_sheet
    deadline = time.monotonic() + wait
    while True:
        sb, dir_b, job_b = ensure_index(request, identity, doc, vb, wait=0.0)
        st, dir_t, job_t = ensure_index(request, identity, doc, vt, wait=0.0)
        if sb == "ready" and st == "ready":
            break
        failed = [x for x in (job_b, job_t) if x is not None and x.state in ("failed", "cancelled")]
        if failed:
            raise APIError(422 if failed[0].state == "failed" else 409, "compare_unavailable",
                           "This workbook could not be prepared for comparison." if failed[0].state == "failed" else "The comparison was cancelled.")
        if time.monotonic() >= deadline:
            return JSONResponse({"state": "building", "complete": False, "progress": {"base": sb, "target": st},
                                 "message": "Preparing both workbooks for a complete comparison…"}, status_code=202)
        time.sleep(0.1)
    cache: dict = request.app.state.__dict__.setdefault("compare_results", {})
    key = (str(identity.tenant_id), str(rows[str(vb)]["sha256"]), str(rows[str(vt)]["sha256"]))
    if key not in cache:
        started = time.monotonic()
        meta_b, meta_t = load_meta(dir_b), load_meta(dir_t)
        result = engine.diff_xlsx_indexed(meta_b, meta_t, lambda i: open_sheet(dir_b, i), lambda i: open_sheet(dir_t, i))
        # Bound process-wide retained comparison results by serialized size as well as entry count.
        # A single bounded result is retained so cell marks remain available for its visible prefix.
        from collections import OrderedDict
        if not isinstance(cache, OrderedDict):
            cache = request.app.state.compare_results = OrderedDict(cache)
        else:
            cache.move_to_end(key) if key in cache else None
        cache[key] = result
        def cache_bytes() -> int:
            return sum(int(item.get("retained_change_bytes", 0)) for item in cache.values())
        while len(cache) > 8 or (len(cache) > 1 and cache_bytes() > 32 * 1024 * 1024):
            cache.popitem(last=False)
        _audit_compare(request, identity, doc, vb, vt, result, time.monotonic() - started)
    return cache[key]


def _audit_compare(request: Request, identity: AuthenticatedIdentity, doc: UUID, vb: UUID, vt: UUID, result: dict[str, Any], seconds: float) -> None:
    """One audit record per completed large comparison (who compared which versions, how much was examined, the outcome)."""
    from packages.audit.chain import append_event
    try:
        with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
            append_event(connection=connection, actor_id=identity.subject, action="document.compare_completed", subject=str(doc),
            payload={"document_id": str(doc), "base_version_id": str(vb), "target_version_id": str(vt), "outcome": result.get("scan_status", "COMPLETE" if result.get("complete", True) else "PARTIAL"),
                                  "list_status": result.get("list_status", "COMPLETE"), "examined_cells": result.get("examined_cells"), "changes": result["summary"]["total"], "seconds": round(seconds, 1)})
    except Exception:  # noqa: BLE001 - the audit write must not break reading a comparison; it is logged
        logging.getLogger("idw.api").error("compare_audit_failed")


def _docx_page_lines(request: Request, identity: AuthenticatedIdentity, doc: UUID, version_id: UUID, sha: str) -> list[dict[str, Any]] | None:
    """Text lines (page, text, box) of the page preview of one DOCX version, cached; None when no preview can be rendered."""
    cache: dict = request.app.state.__dict__.setdefault("docx_page_lines", {})
    key = (str(identity.tenant_id), sha)
    if key in cache:
        return cache[key]
    from packages.api.render import render_cache_path, rendered_pdf
    try:
        rendered_pdf(request, identity, doc, version_id)
        outline = request.app.state.pdf_editor.outline(render_cache_path(request, identity, version_id, sha))
        lines = [{"page": p["page"], "t": ln["t"], "bb": ln.get("bb")} for p in outline.get("pages", []) for ln in p.get("lines", [])]
    except Exception:  # noqa: BLE001 - locating changes on pages is an aid; the comparison stands without it
        lines = None
    if len(cache) > 24:
        cache.clear()
    cache[key] = lines
    return lines


def _locate(lines: list[dict[str, Any]] | None, text: str | None) -> tuple[int | None, list[float] | None]:
    """Page and box of the line where a paragraph starts (best effort, never invented)."""
    import re
    if not lines or not text:
        return None, None
    norm = lambda t: re.sub(r"\W+", "", t or "").lower()  # noqa: E731
    head = norm(text)[:24]
    if len(head) < 4:
        return None, None
    for line in lines:
        n = norm(line["t"])
        if n and (n.startswith(head[: max(4, min(len(n), len(head)))]) or head.startswith(n[:24]) and len(n) >= 4):
            return line["page"], line.get("bb")
    return None, None


def _locate_docx_changes(request: Request, identity: AuthenticatedIdentity, doc: UUID, vb: UUID, vt: UUID, rows: dict[str, dict[str, Any]], diff: dict[str, Any]) -> None:
    base_lines = _docx_page_lines(request, identity, doc, vb, str(rows[str(vb)]["sha256"]))
    target_lines = _docx_page_lines(request, identity, doc, vt, str(rows[str(vt)]["sha256"]))
    for change in diff["changes"]:
        if change["category"] not in ("text", "format"):
            continue
        change["base_page"], change["base_bbox"] = _locate(base_lines, change.get("before"))
        change["target_page"], change["target_bbox"] = _locate(target_lines, change.get("after"))
        change["page"] = change.get("target_page") or change.get("base_page")


@router.get("/documents/{document_id}/compare")
def compare_versions(document_id: str, request: Request, base: str = Query(), target: str = Query(),
                     offset: int = Query(default=0, ge=0), limit: int = Query(default=5000, ge=1, le=5000), wait: float = Query(default=20.0, ge=0, le=60),
                     kind: str | None = Query(default=None, pattern="^(added|removed|modified)$"), category: str | None = Query(default=None, max_length=20),
                     identity: AuthenticatedIdentity = Depends(require_identity)) -> Any:
    try:
        doc, vb, vt = UUID(document_id), UUID(base), UUID(target)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None
    versions, events, meta = load_family(request, identity, doc)
    by_id = {str(v["version_id"]): v for v in versions}
    if str(vb) not in by_id or str(vt) not in by_id:
        raise APIError(404, "not_found", "The requested version was not found.")
    numbers = {str(v["version_id"]): i + 1 for i, v in enumerate(versions)}
    parent = {str(v["version_id"]): (str(v["parent_version_id"]) if v["parent_version_id"] else None) for v in versions}
    classes = lineage_classes(versions, events)
    events_by_version: dict[str, dict[str, Any]] = {}
    for ev in sorted(events, key=lambda e: e["sequence"]):
        pv = (ev.get("payload") or {}).get("version_id")
        if pv and ev["action"] in {"document.text_version_saved", "document.derived_version_released"}:
            events_by_version[str(pv)] = ev
    rel = relation(parent, str(vb), str(vt))
    card = lambda vid: _version_card(numbers[vid], by_id[vid], classes, events_by_version, numbers)  # noqa: E731
    path_cards = [card(v) for v in rel["path"]]
    result: dict[str, Any] = {"document_id": document_id, "name": meta["filename"], "base": card(str(vb)), "target": card(str(vt)),
                              "relation": {**rel, "path_numbers": [numbers[v] for v in rel["path"]]}, "path": path_cards}
    same_file = str(by_id[str(vb)]["sha256"]) == str(by_id[str(vt)]["sha256"])
    result["identical_bytes"] = same_file
    suffix = PurePosixPath(meta["filename"]).suffix.lower()
    if same_file:
        fmt = "xlsx" if suffix == ".xlsx" else "pdf" if suffix == ".pdf" else "docx" if suffix == ".docx" else "text"
        return {**result, "format": fmt, "changes": [], "summary": engine._summary([], False), "limits": [], "complete": True, "page": {"offset": 0, "limit": limit, "total": 0},
                "note": "These two versions are the same file; there is nothing to compare."}
    if suffix == ".xlsx":
        outcome = _xlsx_comparison(request, identity, doc, vb, vt, by_id, wait)
        if isinstance(outcome, JSONResponse):
            return outcome
        note = None if outcome["changes"] else "The files differ at the byte level, but no differences were found in the content that is compared."
        return {**result, "format": "xlsx", **_page(outcome, offset, limit, kind, category), "note": note, "state": "ready"}
    vbase, source_b = _source_version(request, identity, doc, vb)
    vtarget, source_t = _source_version(request, identity, doc, vt)
    fmt_b, snap_b = snapshot(request, identity, doc, vbase, source_b, meta["filename"])
    fmt_t, snap_t = snapshot(request, identity, doc, vtarget, source_t, meta["filename"])
    differ = diff_lines if fmt_b == "text" else engine.DIFFERS[fmt_b]
    diff = differ(snap_b, snap_t)
    if fmt_b == "docx" and diff["changes"]:
        _locate_docx_changes(request, identity, doc, vb, vt, by_id, diff)
    note = None if diff["changes"] else "The files differ at the byte level, but no differences were found in the content that is compared."
    if fmt_b == "pdf":
        result["signature"] = {"base": _sig(source_b), "target": _sig(source_t)}
        result["pages"] = {"base": [{"page": p["page"], "width": p["width"], "height": p["height"]} for p in snap_b["pages"]],
                           "target": [{"page": p["page"], "width": p["width"], "height": p["height"]} for p in snap_t["pages"]]}
    return {**result, "format": fmt_b, **_page(diff, offset, limit, kind, category), "note": note, "state": "ready"}


@router.get("/documents/{document_id}/compare/marks")
def compare_marks(document_id: str, request: Request, base: str = Query(), target: str = Query(), sheet: str = Query(max_length=200),
                  r1: int = Query(ge=1), r2: int = Query(ge=1), c1: int = Query(ge=1), c2: int = Query(ge=1),
                  identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, Any]:
    """Changed cells inside a window of a completed XLSX comparison, for highlighting in the virtual viewer."""
    try:
        doc, vb, vt = UUID(document_id), UUID(base), UUID(target)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None
    versions, _events, _meta = load_family(request, identity, doc)
    by_id = {str(v["version_id"]): v for v in versions}
    if str(vb) not in by_id or str(vt) not in by_id:
        raise APIError(404, "not_found", "The requested version was not found.")
    cache = request.app.state.__dict__.get("compare_results", {})
    key = (str(identity.tenant_id), str(by_id[str(vb)]["sha256"]), str(by_id[str(vt)]["sha256"]))
    if key not in cache:
        raise APIError(409, "comparison_not_ready", "Run the comparison first.")
    marks = [[c["rc"][0], c["rc"][1], c["kind"]] for c in cache[key]["changes"]
             if c.get("rc") and c["location"].get("sheet") == sheet and r1 <= c["rc"][0] <= r2 and c1 <= c["rc"][1] <= c2]
    return {"marks": marks}


def _sig(data: bytes) -> dict[str, Any]:
    info = pdf_signature.classify(data)
    return {"status": info["status"], "validated": False}


@router.get("/documents/{document_id}/versions/{version_id}/changes")
def changes_from_parent(document_id: str, version_id: str, request: Request,
                        identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, Any]:
    """What changed from this version's DIRECT parent (the real lineage, not 'the previous number')."""
    try:
        doc, ver = UUID(document_id), UUID(version_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None
    versions, _, _ = load_family(request, identity, doc)
    item = next((v for v in versions if str(v["version_id"]) == str(ver)), None)
    if item is None:
        raise APIError(404, "not_found", "The requested version was not found.")
    if not item["parent_version_id"]:
        raise APIError(422, "no_parent", "The original has no earlier version to compare with.")
    return compare_versions(document_id, request, base=str(item["parent_version_id"]), target=str(ver), offset=0, limit=5000, wait=20.0, kind=None, category=None, identity=identity)


@router.get("/documents/{document_id}/history")
def version_history(document_id: str, request: Request, identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, Any]:
    """Per-version facts for the History page: lineage class, verification outcome, download eligibility, signature status, audit."""
    try:
        doc = UUID(document_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document was not found.") from None
    versions, events, meta = load_family(request, identity, doc)
    numbers = {str(v["version_id"]): i + 1 for i, v in enumerate(versions)}
    classes = lineage_classes(versions, events)
    events_by_version: dict[str, dict[str, Any]] = {}
    for ev in sorted(events, key=lambda e: e["sequence"]):
        pv = (ev.get("payload") or {}).get("version_id")
        if pv and ev["action"] in {"document.text_version_saved", "document.derived_version_released"}:
            events_by_version[str(pv)] = ev
    is_pdf = PurePosixPath(meta["filename"]).suffix.lower() == ".pdf"
    items = []
    for i, v in enumerate(versions):
        card = _version_card(i + 1, v, classes, events_by_version, numbers)
        card["signature"] = None
        if is_pdf:
            from packages.api.working_copy import signature_payload, validate_signatures
            _version, data = _source_version(request, identity, doc, UUID(str(v["version_id"])))
            info = signature_payload(validate_signatures(request, data, hashlib.sha256(data).hexdigest()))
            card["signature"] = {"state": info["state"], "label": info["label"], "validated": info["validated"], "structure": info["status"]}
        items.append(card)
    return {"document_id": document_id, "name": meta["filename"], "versions": items}
