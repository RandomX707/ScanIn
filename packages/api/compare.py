"""Version comparison API: compares PERSISTED version files (via sandboxed workers), never replayed edit events.

Read-only: no endpoint here can create, modify or delete a document, version or storage object.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

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
    """version_id -> original | derived | unsigned_working_copy (a working copy's descendants stay working copies)."""
    flagged: set[str] = set()
    for event in events:
        payload = event.get("payload") or {}
        if payload.get("unsigned_working_copy") and payload.get("version_id"):
            flagged.add(str(payload["version_id"]))
    parent = {str(v["version_id"]): (str(v["parent_version_id"]) if v["parent_version_id"] else None) for v in versions}
    out: dict[str, str] = {}
    for v in versions:
        vid = str(v["version_id"])
        if v["kind"] == "original":
            out[vid] = "original"
            continue
        node, seen, cls = vid, set(), "derived"
        while node and node not in seen:
            if node in flagged:
                cls = "unsigned_working_copy"
                break
            seen.add(node)
            node = parent.get(node)
        out[vid] = cls
    return out


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


def _snapshot_xlsx(request: Request, path: Path, name: str) -> dict[str, Any]:
    from packages.api.workspace import _all_targets
    report, targets = _all_targets(request, path, name, pages=40)
    if report.status != "inspected":
        raise APIError(422, "compare_unreadable", "This version couldn't be read safely for comparison.")
    layout = (report.details or {}).get("layout") or {}
    styles = layout.get("styles", [])
    layout_sheets = {item["name"]: item for item in layout.get("sheets", [])}
    sheets: dict[str, dict[str, Any]] = {n: {"cells": {}, "styles": {}, "merges": [], "images": [], "cols": None, "rows": None} for n in (report.sheets or [])}
    for t in targets:
        sheet = sheets.setdefault(str(t["sheet"]), {"cells": {}, "styles": {}, "merges": [], "images": [], "cols": None, "rows": None})
        value = t.get("value") or {}
        raw = value.get("value")
        kind = "formula" if str(t.get("reason") or "") in {"formula_cell", "array_formula"} else value.get("type")
        sheet["cells"][str(t["cell"])] = {"v": "" if raw is None else str(raw), "t": kind}
    for n, sheet in sheets.items():
        lay = layout_sheets.get(n) or {}
        for addr, idx in (lay.get("cells") or {}).items():
            if isinstance(idx, int) and 0 <= idx < len(styles) and styles[idx]:
                sheet["styles"][addr] = styles[idx]
        sheet["merges"] = [list(m) for m in lay.get("merges", [])]
        sheet["images"] = sorted(hashlib.sha256(str(i.get("src", "")).encode()).hexdigest()[:16] + f"@{(i.get('from') or {}).get('row')}:{(i.get('from') or {}).get('col')}"
                                 for i in lay.get("images", []))
        sheet["cols"] = lay.get("cols"); sheet["rows"] = lay.get("rows")
    return {"sheets": sheets}


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
        elif suffix == ".xlsx":
            result = ("xlsx", _snapshot_xlsx(request, path, name))
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
    return {"version_id": vid, "number": number, "kind": item["kind"], "classification": classes.get(vid, item["kind"]),
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


@router.get("/documents/{document_id}/compare")
def compare_versions(document_id: str, request: Request, base: str = Query(), target: str = Query(),
                     identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, Any]:
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
    vbase, source_b = _source_version(request, identity, doc, vb)
    vtarget, source_t = _source_version(request, identity, doc, vt)
    result["identical_bytes"] = hashlib.sha256(source_b).hexdigest() == hashlib.sha256(source_t).hexdigest()
    fmt_b, snap_b = snapshot(request, identity, doc, vbase, source_b, meta["filename"])
    if result["identical_bytes"]:
        return {**result, "format": fmt_b, "changes": [], "summary": engine._summary([], False), "limits": [],
                "note": "These two versions are the same file; there is nothing to compare."}
    fmt_t, snap_t = snapshot(request, identity, doc, vtarget, source_t, meta["filename"])
    differ = diff_lines if fmt_b == "text" else engine.DIFFERS[fmt_b]
    diff = differ(snap_b, snap_t)
    note = None if diff["changes"] else "The files differ at the byte level, but no differences were found in the content that is compared."
    if fmt_b == "pdf":
        result["signature"] = {"base": _sig(source_b), "target": _sig(source_t)}
        result["pages"] = {"base": [{"page": p["page"], "width": p["width"], "height": p["height"]} for p in snap_b["pages"]],
                           "target": [{"page": p["page"], "width": p["width"], "height": p["height"]} for p in snap_t["pages"]]}
    return {**result, "format": fmt_b, **diff, "note": note}


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
    return compare_versions(document_id, request, base=str(item["parent_version_id"]), target=str(ver), identity=identity)
