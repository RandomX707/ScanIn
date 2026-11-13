"""Faithful, paginated preview of Office documents (preview only; never an editing source or a saved version)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from threading import BoundedSemaphore, Lock
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from packages.api.auth import AuthenticatedIdentity, require_identity
from packages.api.db_context import api_tenant_transaction
from packages.api.documents import _source_version
from packages.api.errors import APIError
from packages.registry.documents import DocumentRepository
from packages.workers.office_render import OfficeRenderRefused, OfficeRenderUnavailable, SUPPORTED

router = APIRouter(prefix="/api", tags=["render"])
_slots = BoundedSemaphore(2)
_flight_lock = Lock()
_flight: dict[str, Lock] = {}

FIDELITY_NOTE = ("Layout rendered locally by LibreOffice for viewing only. Fonts or effects Word draws differently can "
                 "vary slightly; the original DOCX is never changed.")


def render_cache_path(request: Request, identity: AuthenticatedIdentity, version_id: UUID, sha256: str) -> Path:
    root: Path = request.app.state.document_storage.root
    return root / ".preview-cache" / str(identity.tenant_id) / f"{version_id}-{sha256[:24]}.pdf"


def rendered_pdf(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID) -> tuple[bytes, str, bool]:
    """(pdf bytes, source suffix, served_from_cache). Cache key = version id + content hash, so it can never go stale."""
    version, source = _source_version(request, identity, document_id, version_id)
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        original = DocumentRepository(connection, identity.tenant_id).get_original(document_id)
    name = str(original["filename"] if original else version["filename"])
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in SUPPORTED:
        raise APIError(415, "render_type_unsupported", "A faithful page preview isn't available for this file type.")
    sha = hashlib.sha256(source).hexdigest()
    if sha != str(version["sha256"]):
        raise APIError(409, "integrity_mismatch", "The stored file did not match its recorded fingerprint.")
    cache = render_cache_path(request, identity, version_id, sha)
    if cache.is_file():
        return cache.read_bytes(), suffix, True
    key = str(cache)
    with _flight_lock:
        gate = _flight.setdefault(key, Lock())
    with gate:
        if cache.is_file():
            return cache.read_bytes(), suffix, True
        renderer = request.app.state.office_renderer
        acquired = _slots.acquire(timeout=30)
        if not acquired:
            raise APIError(503, "render_busy", "The page preview is busy. Try again in a moment.")
        try:
            pdf = renderer.render_pdf(source, suffix)
        except OfficeRenderUnavailable:
            raise APIError(503, "render_unavailable", "The page preview is unavailable right now.") from None
        except OfficeRenderRefused as refused:
            raise APIError(422, "render_failed", "This document couldn't be rendered as pages.",
                           [{"reason": refused.reason_code}]) from None
        finally:
            _slots.release()
        cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = cache.with_suffix(f".{os.getpid()}.tmp")
        temp.write_bytes(pdf)
        os.replace(temp, cache)
    return pdf, suffix, False


@router.get("/documents/{document_id}/versions/{version_id}/render")
def render_version(document_id: str, version_id: str, request: Request,
                   identity: AuthenticatedIdentity = Depends(require_identity)) -> Response:
    try:
        parsed_document, parsed_version = UUID(document_id), UUID(version_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None
    pdf, _, cached = rendered_pdf(request, identity, parsed_document, parsed_version)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"X-Render-Engine": "libreoffice-sandboxed", "X-Render-Cache": "hit" if cached else "miss",
                             "X-Render-Note": FIDELITY_NOTE, "Content-Disposition": 'inline; filename="preview.pdf"',
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"})
