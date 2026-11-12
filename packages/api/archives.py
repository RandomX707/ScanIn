"""ZIP archives as a first-class input: every entry is classified, safely expanded and imported as its own document.

An archive is never stored or edited as one opaque document. Each accepted child goes through exactly the same
scan -> private immutable store -> sandboxed inspection path as a normal upload; the archive path is kept as
provenance in the audit chain and surfaced on the document folder.
"""
from __future__ import annotations

import hashlib
import tempfile
from collections import Counter
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from packages.api.auth import AuthenticatedIdentity, require_identity
from packages.api.db_context import api_tenant_transaction
from packages.api.documents import _filename, ingest_stream, inspect_document
from packages.api.errors import APIError
from packages.ingestion import archive as A
from packages.registry.documents import DocumentRepository

router = APIRouter(prefix="/api/archives", tags=["archives"])

# Plain sentences for the codes ingest/inspect can raise on a child.
CHILD_ERRORS = {
    "upload_too_large": "This file is too large.",
    "unknown_file_type": "This file type couldn't be recognised.",
    "executable_content": "This file looks like a program, so it was blocked.",
}


def _child_error(error: APIError) -> tuple[str, str]:
    code = str(getattr(error, "code", "") or "failed")
    if getattr(error, "status_code", 0) == 422 and code not in CHILD_ERRORS:
        return "blocked", CHILD_ERRORS.get(code, "This file didn't pass the safety checks.")
    return "failed", CHILD_ERRORS.get(code, "This file couldn't be imported. Try it on its own.")


def _import(request: Request, identity: AuthenticatedIdentity, upload, archive_name: str, duplicates: str) -> dict[str, object]:
    storage = request.app.state.document_storage
    scanner = request.app.state.ingestion_pipeline.scanner
    max_bytes = request.app.state.settings.max_upload_bytes
    staged = storage.stage(identity.tenant_id, uuid4(), uuid4(), upload, max_bytes=max_bytes)
    try:
        try:
            scan = scanner.scan(staged.path)
        except Exception:  # noqa: BLE001 - a scanner failure fails closed, no details leaked
            scan = None
        if scan is None or scan.status not in {"clean", "infected"}:
            raise APIError(503, "scan_not_verified", "The archive couldn't be safety-checked right now. Try again in a moment.")
        if scan.status == "infected":
            raise APIError(422, "archive_blocked", "This archive didn't pass the safety scan, so nothing was imported.")
        archive_sha = staged.sha256
        try:
            with open(staged.path, "rb") as handle:
                planned = A.plan(handle)
        except A.ArchiveRejected as rejected:
            raise APIError(422, rejected.code, rejected.message) from None
        budget = A.Budget()
        report: list[dict[str, object]] = []
        with open(staged.path, "rb") as handle:
            zf = A.open_archive(handle)
            for entry in planned:
                row = entry.public()
                row.update({"document_id": None, "version_id": None, "sha256": None, "existing_document_id": None})
                if entry.status != "accept":
                    report.append(row)
                    continue
                try:
                    data = A.read_entry(zf, entry, budget)
                except A.ArchiveRejected as rejected:
                    row.update({"status": "blocked", "reason": rejected.code, "reason_text": A.REASONS.get(rejected.code, "This file couldn't be extracted.")})
                    report.append(row)
                    continue
                sha = hashlib.sha256(data).hexdigest()
                row["sha256"] = sha
                provenance = {"archive_name": archive_name, "archive_sha256": archive_sha, "entry_path": entry.path,
                              "entry_sha256": sha}
                try:
                    existing = None
                    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
                        existing = DocumentRepository(connection, identity.tenant_id).find_original_by_sha(sha) if duplicates == "link" else None
                        if existing is not None:
                            DocumentRepository(connection, identity.tenant_id).record_archive_provenance(
                                document_id=existing["document_id"], actor_id=identity.subject, payload={**provenance, "linked": True})
                    if existing is not None:
                        row.update({"status": "linked_duplicate", "reason": "duplicate_of_existing", "document_id": str(existing["document_id"]),
                                    "existing_document_id": str(existing["document_id"]), "version_id": str(existing["version_id"]),
                                    "reason_text": "Already in your workspace, so it was linked instead of added again."})
                        report.append(row)
                        continue
                    import io
                    registered = ingest_stream(request, identity, _filename(entry.name), io.BytesIO(data))
                    inspected = inspect_document(str(registered["document_id"]), request, False, identity)
                    if isinstance(inspected, dict) and inspected.get("state") not in (None, "ready"):
                        # Same rule as a single upload: a file that can't be read safely is not a document.
                        row.update({"status": "failed", "reason": "unreadable", "reason_text": "This file couldn't be read safely, so it wasn't added."})
                        report.append(row)
                        continue
                    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
                        DocumentRepository(connection, identity.tenant_id).record_archive_provenance(
                            document_id=registered["document_id"], actor_id=identity.subject, payload={**provenance, "linked": False})
                    row.update({"status": "imported", "document_id": str(registered["document_id"]), "version_id": str(registered["version_id"])})
                except APIError as error:
                    status, text = _child_error(error)
                    row.update({"status": status, "reason": str(error.code), "reason_text": text})
                except Exception:  # noqa: BLE001 - one bad child never aborts the rest, and no internals leak
                    row.update({"status": "failed", "reason": "import_failed", "reason_text": "This file couldn't be imported. Try it on its own."})
                report.append(row)
        counts = Counter(str(r["status"]) for r in report)
        summary = {"entries": len(report), "imported": counts["imported"], "linked_duplicate": counts["linked_duplicate"],
                   "skipped": counts["skipped"], "blocked": counts["blocked"], "failed": counts["failed"],
                   "recognized": counts["imported"] + counts["linked_duplicate"] + counts["failed"]}
        try:
            with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
                DocumentRepository(connection, identity.tenant_id).record_archive_import(
                    actor_id=identity.subject, subject=archive_sha, payload={"archive_name": archive_name, "size_bytes": staged.size_bytes,
                                                                             "duplicates": duplicates, **summary})
        except Exception:  # noqa: BLE001 - the documents are already recorded individually
            pass
        return {"archive": {"name": archive_name, "sha256": archive_sha, "size_bytes": staged.size_bytes}, "summary": summary, "entries": report}
    finally:
        try:
            storage.discard_staged(staged)
        except Exception:  # noqa: BLE001
            pass


@router.put("")
async def import_archive(request: Request, filename: str = Query(min_length=1, max_length=1024),
                         duplicates: str = Query(default="link", pattern="^(link|import)$"),
                         identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Expand a ZIP: classify every entry, import supported documents, report each outcome (nothing is silently dropped)."""
    name = _filename(filename)
    if not name.lower().endswith(".zip"):
        raise APIError(415, "not_an_archive", "Only ZIP archives can be imported this way.")
    settings = request.app.state.settings
    with tempfile.TemporaryFile(mode="w+b") as upload:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > settings.max_upload_bytes:
                raise APIError(413, "upload_too_large", "The upload exceeds the configured limit.")
            upload.write(chunk)
        if size == 0:
            raise APIError(422, "empty_upload", "The uploaded file is empty.")
        upload.seek(0)
        return await run_in_threadpool(_import, request, identity, upload, name, duplicates)
