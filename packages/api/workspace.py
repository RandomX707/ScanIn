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
from packages.api.documents import _filename, ingest_stream, inspect_document
from packages.api.errors import APIError
from packages.registry.documents import DocumentRepository
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
