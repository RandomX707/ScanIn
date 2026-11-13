"""Windowed access to large workbooks.

A sandboxed worker parses a workbook version ONCE into a per-sheet SQLite index (every cell the verified adapter reports, plus
styled-only cells, plus the unclipped layout). The browser then asks for rectangular windows by sheet and row/column range, so it
only ever renders and transfers what is on screen, while nothing about the workbook is dropped.

The index is keyed by version id + content hash (it can never describe a different file), lives under
`<storage>/.xlsx-index/<tenant>/`, and is derived data: deleting it only costs a rebuild. Builds are bounded, single-flight per
version, cancellable and reported as `building` until they finish; no endpoint ever returns a half-built or partial index.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from packages.api.auth import AuthenticatedIdentity, require_identity
from packages.api.db_context import api_tenant_transaction
from packages.api.errors import APIError
from packages.registry.documents import DocumentRepository
from packages.workers.xlsx_index import RestrictedXlsxIndexer, XlsxIndexUnavailable

router = APIRouter(prefix="/api", tags=["sheet-index"])
log = logging.getLogger("idw.api")
MAX_WINDOW_ROWS, MAX_WINDOW_COLS, MAX_WINDOW_CELLS = 200, 80, 20_000
VIRTUAL_XML_BYTES = 1_500_000        # worksheet XML bigger than this (uncompressed) is shown through the windowed viewer


@dataclass
class IndexJob:
    key: tuple[str, str, str]
    state: str = "building"                      # building | ready | failed | cancelled
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)


class SheetIndexManager:
    def __init__(self, indexer: RestrictedXlsxIndexer, workers: int = 3):
        self.indexer = indexer
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="xlsx-index")
        self.jobs: dict[tuple[str, str, str], IndexJob] = {}
        self.lock = threading.Lock()

    def start(self, key: tuple[str, str, str], source_path: Path, final_dir: Path) -> IndexJob:
        with self.lock:
            job = self.jobs.get(key)
            if job is not None and job.state == "building":
                return job
            if (final_dir / "meta.json").is_file():
                job = IndexJob(key, state="ready"); job.finished = job.started; self.jobs[key] = job
                return job
            job = IndexJob(key); self.jobs[key] = job
            if len(self.jobs) > 256:
                for stale in [k for k, j in self.jobs.items() if j.state != "building"][:128]:
                    self.jobs.pop(stale, None)
        self.pool.submit(self._run, job, source_path, final_dir)
        return job

    def _run(self, job: IndexJob, source_path: Path, final_dir: Path) -> None:
        try:
            self.indexer.build(source_path, final_dir, cancel=job.cancel)
            job.state = "ready"
        except XlsxIndexUnavailable as failure:
            job.state, job.error = ("cancelled" if failure.reason_code == "worker_cancelled" else "failed"), failure.reason_code
        except Exception as error:  # noqa: BLE001 - a job failure must never take the API down
            job.state, job.error = "failed", type(error).__name__
        job.finished = time.monotonic()
        log.info("xlsx_index state=%s seconds=%.1f", job.state, job.finished - job.started)

    def cancel(self, key: tuple[str, str, str]) -> bool:
        job = self.jobs.get(key)
        if job is None or job.state != "building":
            return False
        job.cancel.set()
        return True


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None


def version_row(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID) -> tuple[dict[str, Any], str]:
    """Tenant-scoped version row (no file read). Returns (row, original filename)."""
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        document = repo.get(document_id)
        version = repo.get_version(document_id, version_id)
        original = repo.get_original(document_id)
    if document is None or version is None:
        raise APIError(404, "not_found", "The requested document version was not found.")
    if document["ingestion_state"] != "ready":
        raise APIError(409, "document_not_ready", "The document is not ready.")
    return dict(version), str(original["filename"] if original else version["filename"])


def index_dir(request: Request, identity: AuthenticatedIdentity, version_id: UUID, sha256: str) -> Path:
    return request.app.state.document_storage.root / ".xlsx-index" / str(identity.tenant_id) / f"{version_id}-{sha256[:24]}"


def ensure_index(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID, *, wait: float = 0.0) -> tuple[str, Path, IndexJob | None]:
    """('ready'|'building'|'failed'|'cancelled', dir, job). Waits up to `wait` seconds for a build in progress."""
    version, name = version_row(request, identity, document_id, version_id)
    if PurePosixPath(name).suffix.lower() != ".xlsx":
        raise APIError(415, "sheet_index_xlsx_only", "Windowed access is available for XLSX workbooks.")
    directory = index_dir(request, identity, version_id, str(version["sha256"]))
    if (directory / "meta.json").is_file():
        return "ready", directory, None
    manager: SheetIndexManager = request.app.state.sheet_index
    key = (str(identity.tenant_id), str(version_id), str(version["sha256"]))
    source = request.app.state.document_storage.root / str(version["storage_key"])
    job = manager.start(key, source, directory)
    deadline = time.monotonic() + wait
    while job.state == "building" and time.monotonic() < deadline:
        time.sleep(0.05)
    if job.state == "ready" and (directory / "meta.json").is_file():
        return "ready", directory, job
    return job.state if job.state != "ready" else "building", directory, job


def load_meta(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "meta.json").read_text(encoding="utf-8"))


def open_sheet(directory: Path, index: int) -> sqlite3.Connection:
    path = directory / f"sheet-{index}.sqlite"
    if not path.is_file():
        raise APIError(404, "not_found", "That worksheet was not found.")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _public_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {"state": "ready", "total_cells": meta["total_cells"], "capability": meta["capability"], "styles": meta["styles"],
            "sheets": [{k: v for k, v in s.items()} for s in meta["sheets"]]}


def _sheet_index(meta: dict[str, Any], sheet: str) -> int:
    for item in meta["sheets"]:
        if item["name"] == sheet:
            return int(item["index"])
    raise APIError(404, "not_found", "That worksheet was not found.")


@router.get("/documents/{document_id}/versions/{version_id}/sheet/meta")
def sheet_meta(document_id: str, version_id: str, request: Request, wait: float = Query(default=2.0, ge=0, le=20),
               identity: AuthenticatedIdentity = Depends(require_identity)) -> JSONResponse:
    state, directory, job = ensure_index(request, identity, _uuid(document_id), _uuid(version_id), wait=wait)
    if state == "ready":
        return JSONResponse(_public_meta(load_meta(directory)))
    body = {"state": state, "error": job.error if job else None, "elapsed_s": round(time.monotonic() - job.started, 1) if job else 0}
    return JSONResponse(body, status_code=202 if state == "building" else 409)


@router.post("/documents/{document_id}/versions/{version_id}/sheet/index")
def start_index(document_id: str, version_id: str, request: Request, identity: AuthenticatedIdentity = Depends(require_identity)) -> JSONResponse:
    state, _, job = ensure_index(request, identity, _uuid(document_id), _uuid(version_id), wait=0.0)
    return JSONResponse({"state": state, "error": job.error if job else None}, status_code=200 if state == "ready" else 202)


@router.delete("/documents/{document_id}/versions/{version_id}/sheet/index")
def cancel_index(document_id: str, version_id: str, request: Request, identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, Any]:
    doc, ver = _uuid(document_id), _uuid(version_id)
    version, _ = version_row(request, identity, doc, ver)
    key = (str(identity.tenant_id), str(ver), str(version["sha256"]))
    return {"cancelled": request.app.state.sheet_index.cancel(key)}


def _reason(code: str | None) -> str | None:
    if not code:
        return None
    from packages.api.workspace import cell_reason
    return cell_reason(code)


@router.get("/documents/{document_id}/versions/{version_id}/sheet/window")
def sheet_window(document_id: str, version_id: str, request: Request, sheet: str = Query(max_length=200),
                 r1: int = Query(ge=1), r2: int = Query(ge=1), c1: int = Query(ge=1), c2: int = Query(ge=1),
                 identity: AuthenticatedIdentity = Depends(require_identity)) -> JSONResponse:
    """Cells in the rectangle [r1..r2] x [c1..c2] as [row, col, value, type, style, editable, reason]. Bounded; never partial for the window."""
    if r2 < r1 or c2 < c1 or r2 - r1 + 1 > MAX_WINDOW_ROWS or c2 - c1 + 1 > MAX_WINDOW_COLS:
        raise APIError(422, "window_invalid", "That window is too large or reversed.")
    state, directory, _job = ensure_index(request, identity, _uuid(document_id), _uuid(version_id), wait=0.0)
    if state != "ready":
        return JSONResponse({"state": state}, status_code=202 if state == "building" else 409)
    meta = load_meta(directory)
    db = open_sheet(directory, _sheet_index(meta, sheet))
    try:
        rows = db.execute("SELECT r,c,v,t,s,e,rs FROM cells WHERE r BETWEEN ? AND ? AND c BETWEEN ? AND ? ORDER BY r,c LIMIT ?",
                          (r1, r2, c1, c2, MAX_WINDOW_CELLS + 1)).fetchall()
    finally:
        db.close()
    if len(rows) > MAX_WINDOW_CELLS:
        raise APIError(422, "window_invalid", "That window holds too many cells.")
    return JSONResponse({"state": "ready", "sheet": sheet, "r1": r1, "r2": r2, "c1": c1, "c2": c2,
                         "cells": [[r, c, v, t, s, e, _reason(rs)] for r, c, v, t, s, e, rs in rows]})


@router.get("/documents/{document_id}/versions/{version_id}/sheet/cell")
def sheet_cell(document_id: str, version_id: str, request: Request, sheet: str = Query(max_length=200), row: int = Query(ge=1), col: int = Query(ge=1),
               identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, Any]:
    """Full detail of one cell, including the edit fingerprint the save flow needs."""
    state, directory, _ = ensure_index(request, identity, _uuid(document_id), _uuid(version_id), wait=0.0)
    if state != "ready":
        raise APIError(409, "index_not_ready", "The workbook is still being prepared.")
    meta = load_meta(directory)
    db = open_sheet(directory, _sheet_index(meta, sheet))
    try:
        found = db.execute("SELECT v,t,s,id,fp,e,rs FROM cells WHERE r=? AND c=?", (row, col)).fetchone()
    finally:
        db.close()
    if found is None or not found[3]:
        raise APIError(404, "not_found", "That cell has no editable value.")
    v, t, s, ident, fp, e, rs = found
    return {"id": ident, "fp": fp, "v": v, "t": t, "e": bool(e), "r": _reason(rs)}
