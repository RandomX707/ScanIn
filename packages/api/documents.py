from __future__ import annotations

import tempfile
import io
import json
from urllib.parse import quote
from uuid import UUID, uuid4
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from packages.api.auth import AuthenticatedIdentity, require_identity
from packages.api.db_context import api_tenant_transaction
from packages.api.errors import APIError
from packages.ingestion.pipeline import IngestionOrchestrator
from packages.registry.documents import DocumentRepository
from packages.storage.local import ImmutableLocalStorage, StoredFile
from packages.workers.parser import ParserNotVerified
from packages.core.models.contracts import TypedValue
from packages.db.worker_context import ValidatedJobContext, worker_tenant_transaction
from packages.workers.lifecycle import WorkerLifecycleRepository
from packages.core.adapters.csv_adapter.adapter import CsvAdapter
from packages.core.adapters.json_adapter.adapter import JsonAdapter
from packages.core.adapters.xlsx.adapter import XlsxAdapter
from packages.core.adapters.pdf.adapter import PdfAdapter
from packages.core.adapters.docx.adapter import DocxAdapter
from packages.core.models.contracts import Patch
from packages.core.verification.text_adapters import CsvIndependentVerifier, JsonIndependentVerifier
from packages.core.verification.pdf import PdfIndependentVerifier
from packages.core.verification.docx import DocxIndependentVerifier
from packages.core.verification.independent import IndependentVerifier
from packages.workflow.frozen_serialization import deserialize_frozen_edit_set, serialize_frozen_edit_set
from packages.workflow.release import NativeReleaseWorkflow, WorkflowRejected, XlsxReleaseWorkflow
from packages.ingestion.models import ScanResult
from packages.core.planning.planner import PlanError

router = APIRouter(prefix="/api/documents", tags=["documents"])


class ManualFieldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(min_length=1, max_length=500)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class DataCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corrected_value: dict[str, Any]
    reason: str = Field(min_length=1, max_length=2000)


class CategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(pattern=r"^(insurer-authored|customer-form|third-party-issued|signed-legal|unknown)$")


class EditSetDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=2000)


class PatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(min_length=1, max_length=500)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    new_value: dict[str, Any]


class PatchBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patches: list[PatchInput] = Field(min_length=1, max_length=200)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern=r"^(approved|rejected)$")
    note: str = Field(default="", max_length=2000)


def _require_processor(identity: AuthenticatedIdentity) -> None:
    if identity.role not in {"processor", "administrator"}:
        raise APIError(403, "role_not_permitted", "A processor role is required for this correction.")


def _typed_correction(value: dict[str, Any], *, allow_metadata: bool = False) -> TypedValue:
    kind = value.get("type")
    allowed = {"type", "value", "native", "display"} if allow_metadata else {"type", "value"}
    if not {"type", "value"} <= set(value) or set(value) - allowed:
        raise APIError(422, "invalid_correction_value", "A correction must contain only type and value.")
    try:
        if kind == "decimal":
            from decimal import Decimal
            typed = TypedValue(kind, Decimal(str(value["value"])), native=value.get("native"), display=value.get("display"))
        elif kind == "integer":
            raw = value["value"]
            if isinstance(raw, bool) or not str(raw).lstrip("-").isdigit():
                raise ValueError("invalid_integer")
            typed = TypedValue(kind, int(raw), native=value.get("native"), display=value.get("display"))
        else:
            typed = TypedValue(kind, value["value"], native=value.get("native"), display=value.get("display"))
        if kind == "date":
            from datetime import date
            date.fromisoformat(str(value["value"]))
        if kind == "datetime":
            from datetime import datetime
            datetime.fromisoformat(str(value["value"]))
    except (ValueError, TypeError, KeyError):
        raise APIError(422, "invalid_correction_value", "The correction type and value are incompatible.") from None
    return typed


def _advance_worker_state(request: Request, identity: AuthenticatedIdentity, document_id: UUID,
                          document_tenant_id: UUID, outcome: str) -> str:
    worker_engine = getattr(request.app.state, "worker_engine", None)
    if worker_engine is None:
        raise APIError(503, "worker_database_unconfigured", "The isolated worker database role is not configured.")
    try:
        job = ValidatedJobContext(identity.tenant_id, document_tenant_id)
        with worker_engine.connect() as connection, worker_tenant_transaction(connection, job):
            return WorkerLifecycleRepository(connection, job).transition(document_id, outcome=outcome,
                                                                         actor_id=identity.subject)
    except PermissionError:
        raise APIError(403, "worker_tenant_mismatch", "The document does not belong to the authenticated tenant.") from None
    except (SQLAlchemyError, RuntimeError, ValueError):
        raise APIError(503, "worker_lifecycle_unavailable", "The inspection lifecycle could not be updated.") from None


def _source_version(request: Request, identity: AuthenticatedIdentity, document_id: UUID,
                    version_id: UUID) -> tuple[dict[str, object], bytes]:
    storage: ImmutableLocalStorage = request.app.state.document_storage
    with request.app.state.engine.connect() as connection:
        with api_tenant_transaction(connection, identity):
            repo = DocumentRepository(connection, identity.tenant_id)
            document = repo.get(document_id)
            version = repo.get_version(document_id, version_id)
            if document is None or version is None:
                raise APIError(404, "not_found", "The requested document version was not found.")
            if document["ingestion_state"] != "ready":
                raise APIError(409, "document_not_ready", "The document is not ready for editing.")
            stored = StoredFile(identity.tenant_id, document_id, storage.root / str(version["storage_key"]),
                                str(version["storage_key"]), int(version["size_bytes"]), str(version["sha256"]))
            source = storage.read_original(stored)
    return version, source


def _presentation_capability(report: object) -> dict[str, object]:
    capability = dict(getattr(report, "capability"))
    if getattr(report, "adapter") == "docx":
        capability["level"] = "read_only"
        capability["reasons"] = list(capability.get("reasons", [])) + ["critical_visual_verifier_unavailable"]
    return capability


def _release_workflow(adapter_name: str) -> NativeReleaseWorkflow:
    if adapter_name == "csv":
        return NativeReleaseWorkflow(CsvAdapter(), CsvIndependentVerifier())
    if adapter_name == "json":
        return NativeReleaseWorkflow(JsonAdapter(), JsonIndependentVerifier())
    if adapter_name == "xlsx":
        return XlsxReleaseWorkflow()
    if adapter_name == "pdf":
        return NativeReleaseWorkflow(PdfAdapter(), PdfIndependentVerifier())
    if adapter_name == "docx":
        return NativeReleaseWorkflow(DocxAdapter(), DocxIndependentVerifier())
    raise APIError(415, "native_adapter_unsupported", "This document format does not support native editing.")


def _filename(value: str) -> str:
    # Treat both slash conventions as path separators; never persist a client path.
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or len(name) > 255 or any(ord(char) < 32 for char in name):
        raise APIError(422, "invalid_filename", "A valid filename is required.")
    return name


@router.put("")
async def upload_document(
    request: Request,
    filename: str = Query(min_length=1, max_length=1024),
    identity: AuthenticatedIdentity = Depends(require_identity),
) -> dict[str, object]:
    """Stream a bounded raw upload, scan it, and register only a clean original.

    The document remains private and is not served as a normal viewer until a
    sandboxed inspection path and lifecycle transition authorize it.
    """
    safe_name = _filename(filename)
    settings = request.app.state.settings
    storage: ImmutableLocalStorage = request.app.state.document_storage
    pipeline: IngestionOrchestrator = request.app.state.ingestion_pipeline
    document_id, upload_id = uuid4(), uuid4()
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
        try:
            result = pipeline.process(identity.tenant_id, document_id, upload_id, upload, filename=safe_name)
        except Exception as error:
            # Keep operational details and local paths out of API responses/logs.
            if getattr(error, "reason_code", "") == "upload_too_large":
                raise APIError(413, "upload_too_large", "The upload exceeds the configured limit.") from None
            raise APIError(503, "ingestion_unavailable", "The file could not be safely processed.") from None

    if result.outcome != "stored_private" or result.stored_file is None or result.scan is None:
        if result.outcome in {"rejected", "quarantined"}:
            raise APIError(422, result.detection.reasons[0], "The file did not pass ingestion checks.")
        raise APIError(503, "scan_not_verified", "The file remains private pending security checks.")

    # The ingestion pipeline may store only after a clean digest-matched scan.
    # Parsing is independently sandboxed and fail-closed; a successful upload
    # does not by itself make a document viewable.
    try:
        with request.app.state.engine.connect() as connection:
            with api_tenant_transaction(connection, identity):
                registered = DocumentRepository(connection, identity.tenant_id).register_clean_original(
                    filename=safe_name,
                    media_type=result.detection.media_type,
                    stored=result.stored_file,
                    actor_id=identity.subject,
                )
    except (SQLAlchemyError, RuntimeError, PermissionError, ValueError):
        raise APIError(503, "document_registration_unavailable", "The safely scanned file could not be registered.") from None

    return {
        "document_id": str(registered.document_id),
        "version_id": str(registered.version_id),
        "filename": registered.filename,
        "sha256": registered.source_sha256,
        "size_bytes": registered.size_bytes,
        "state": "staged_private",
        "message": "Upload scanned and stored privately; inspection is required before viewing.",
    }


@router.get("")
def list_documents(
    request: Request,
    identity: AuthenticatedIdentity = Depends(require_identity),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    with request.app.state.engine.connect() as connection:
        with api_tenant_transaction(connection, identity):
            items = DocumentRepository(connection, identity.tenant_id).list_recent(limit=limit)
    return {"items": [{key: str(value) if key in {"id", "current_version_id"} and value is not None else value
                       for key, value in item.items()} for item in items]}


@router.get("/{document_id}/original")
def download_original(
    document_id: str,
    request: Request,
    identity: AuthenticatedIdentity = Depends(require_identity),
) -> Response:
    from uuid import UUID

    try:
        parsed_id = UUID(document_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document was not found.") from None
    storage: ImmutableLocalStorage = request.app.state.document_storage
    with request.app.state.engine.connect() as connection:
        with api_tenant_transaction(connection, identity):
            row = DocumentRepository(connection, identity.tenant_id).get_original(parsed_id)
            if row is None or row.get("storage_key") is None:
                raise APIError(404, "not_found", "The requested document was not found.")
            stored = StoredFile(identity.tenant_id, parsed_id, storage.root / str(row["storage_key"]),
                                str(row["storage_key"]), int(row["size_bytes"]), str(row["sha256"]))
            content = storage.read_original(stored)
            filename = _filename(str(row["filename"]))
            media_type = str(row.get("mime") or "application/octet-stream")
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
                             "X-Content-Type-Options": "nosniff"})


@router.post("/{document_id}/inspect")
def inspect_document(
    document_id: str,
    request: Request,
    include_targets: bool = Query(default=False),
    identity: AuthenticatedIdentity = Depends(require_identity),
) -> dict[str, object]:
    """Inspect a clean immutable version in the isolated parser boundary."""

    try:
        parsed_id = UUID(document_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document was not found.") from None
    storage: ImmutableLocalStorage = request.app.state.document_storage
    with request.app.state.engine.connect() as connection:
        with api_tenant_transaction(connection, identity):
            row = DocumentRepository(connection, identity.tenant_id).get(parsed_id)
            if row is None or row.get("storage_key") is None:
                raise APIError(404, "not_found", "The requested document was not found.")
            stored = StoredFile(identity.tenant_id, parsed_id, storage.root / str(row["storage_key"]),
                                str(row["storage_key"]), int(row["size_bytes"]), str(row["sha256"]))
            storage.read_original(stored)
            filename = _filename(str(row["filename"]))
            document_tenant_id = UUID(str(row["tenant_id"]))
    state = _advance_worker_state(request, identity, parsed_id, document_tenant_id, "begin")
    try:
        report = request.app.state.restricted_parser.inspect(stored.path, filename=filename,
                                                             include_targets=include_targets)
    except ParserNotVerified:
        raise APIError(503, "inspection_not_verified", "Restricted document inspection is unavailable.") from None
    if report.status == "rejected":
        try:
            state = _advance_worker_state(request, identity, parsed_id, document_tenant_id, "failed")
        except (SQLAlchemyError, RuntimeError, PermissionError, ValueError):
            raise APIError(503, "inspection_state_unavailable", "Inspection status could not be recorded.") from None
    else:
        try:
            state = _advance_worker_state(request, identity, parsed_id, document_tenant_id, "ready")
        except (SQLAlchemyError, RuntimeError, PermissionError, ValueError):
            raise APIError(503, "inspection_state_unavailable", "Inspection status could not be recorded.") from None
    return {
        "document_id": str(parsed_id), "filename": filename, "source_sha256": report.sha256,
        "size_bytes": report.size_bytes, "media_type": report.media_type,
        "capability": _presentation_capability(report), "adapter": report.adapter,
        "adapter_version": report.adapter_version, "sheets": list(report.sheets),
        "features": list(report.features), "findings": list(report.findings),
        "inspection_score": report.score, "state": state, "viewing_ready": state == "ready",
        "targets": list(report.targets), "targets_truncated": report.targets_truncated, "details": report.details,
    }


@router.get("/{document_id}/versions")
def list_document_versions(document_id: str, request: Request,
                           identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    try:
        parsed_id = UUID(document_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document was not found.") from None
    with request.app.state.engine.connect() as connection:
        with api_tenant_transaction(connection, identity):
            repo = DocumentRepository(connection, identity.tenant_id)
            if repo.get(parsed_id) is None:
                raise APIError(404, "not_found", "The requested document was not found.")
            versions = repo.list_versions(parsed_id)
    return {"items": jsonable_encoder(versions)}


@router.get("/{document_id}/versions/{version_id}/view")
def view_document_version(document_id: str, version_id: str, request: Request,
                          identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    try:
        parsed_id, parsed_version_id = UUID(document_id), UUID(version_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None
    storage: ImmutableLocalStorage = request.app.state.document_storage
    with request.app.state.engine.connect() as connection:
        with api_tenant_transaction(connection, identity):
            repo = DocumentRepository(connection, identity.tenant_id)
            row = repo.get_version(parsed_id, parsed_version_id)
            if row is None:
                raise APIError(404, "not_found", "The requested version was not found.")
            if row["ingestion_state"] != "ready":
                raise APIError(409, "document_not_ready", "Safe inspection must complete before viewing.")
            stored = StoredFile(identity.tenant_id, parsed_id, storage.root / str(row["storage_key"]),
                                str(row["storage_key"]), int(row["size_bytes"]), str(row["sha256"]))
            source = storage.read_original(stored)
            filename = _filename(str(row["filename"]))
    try:
        report = request.app.state.restricted_parser.inspect(stored.path, filename=filename, include_targets=True)
    except ParserNotVerified:
        raise APIError(503, "view_not_verified", "Safe document viewing is unavailable.") from None
    if report.sha256 != row["sha256"]:
        raise APIError(409, "source_version_changed", "The requested version failed integrity validation.")
    if report.adapter not in {"csv", "json", "xlsx", "pdf", "docx"} or report.status != "inspected":
        raise APIError(415, "viewer_not_supported", "This file type has no supported read-only viewer.")
    if report.targets_truncated:
        raise APIError(413, "viewer_target_limit", "The document exceeds the safe viewer target limit.")
    return {
        "document_id": str(parsed_id), "version_id": str(parsed_version_id),
        "parent_version_id": str(row["parent_version_id"]) if row["parent_version_id"] else None,
        "version_kind": row["kind"], "filename": filename, "source_sha256": report.sha256,
        "size_bytes": len(source), "media_type": report.media_type,
        "capability": _presentation_capability(report), "adapter": report.adapter,
        "features": list(report.features), "targets": list(report.targets), "details": report.details,
        "structural_summary": ({**report.structural_summary, "source_document_id": str(parsed_id),
                                "source_version_id": str(parsed_version_id)} if report.structural_summary else None),
    }


@router.post("/{document_id}/fields")
def create_manual_field(document_id: str, payload: ManualFieldRequest, request: Request,
                        identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Bind a user-selected field to a current, parser-discovered native target."""
    _require_processor(identity)
    try:
        parsed_id = UUID(document_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document was not found.") from None
    storage: ImmutableLocalStorage = request.app.state.document_storage
    with request.app.state.engine.connect() as connection:
        with api_tenant_transaction(connection, identity):
            repo = DocumentRepository(connection, identity.tenant_id)
            row = repo.get(parsed_id)
            if row is None or row.get("storage_key") is None:
                raise APIError(404, "not_found", "The requested document was not found.")
            if row["ingestion_state"] != "ready":
                raise APIError(409, "document_not_ready", "The document must finish safe inspection first.")
            stored = StoredFile(identity.tenant_id, parsed_id, storage.root / str(row["storage_key"]),
                                str(row["storage_key"]), int(row["size_bytes"]), str(row["sha256"]))
            storage.read_original(stored)
            version_id = UUID(str(row["current_version_id"]))
            filename = _filename(str(row["filename"]))
    try:
        report = request.app.state.restricted_parser.inspect(stored.path, filename=filename, include_targets=True)
    except ParserNotVerified:
        raise APIError(503, "target_resolution_not_verified", "Safe target resolution is unavailable.") from None
    if report.sha256 != stored.sha256:
        raise APIError(409, "source_version_changed", "The source version changed during target resolution.")
    target = next((item for item in report.targets if item.get("id") == payload.target_id), None)
    if target is None or target.get("editable") is not True or target.get("fingerprint") != payload.fingerprint:
        raise APIError(409, "target_not_editable_or_stale", "The selected field is unsupported or no longer current.")
    label = str(target.get("label") or payload.target_id)
    try:
        with request.app.state.engine.connect() as connection:
            with api_tenant_transaction(connection, identity):
                repo = DocumentRepository(connection, identity.tenant_id)
                current = repo.get(parsed_id)
                if current is None or str(current.get("current_version_id")) != str(version_id) or current.get("sha256") != stored.sha256:
                    raise APIError(409, "source_version_changed", "The source version changed during target resolution.")
                field_id = repo.create_manual_field(document_id=parsed_id, version_id=version_id,
                    target_id=payload.target_id, fingerprint=payload.fingerprint, label=label,
                    observed_value=target["value"], actor_id=identity.subject)
    except (SQLAlchemyError, RuntimeError, PermissionError, ValueError):
        raise APIError(503, "field_registration_unavailable", "The selected field could not be recorded.") from None
    return {"field_id": str(field_id), "document_id": str(parsed_id), "target_id": payload.target_id,
            "label": label, "observed_value": target["value"], "source_version_id": str(version_id)}


@router.get("/{document_id}/fields")
def list_manual_fields(document_id: str, request: Request,
                       identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    try:
        parsed_id = UUID(document_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document was not found.") from None
    with request.app.state.engine.connect() as connection:
        with api_tenant_transaction(connection, identity):
            repo = DocumentRepository(connection, identity.tenant_id)
            if repo.get(parsed_id) is None:
                raise APIError(404, "not_found", "The requested document was not found.")
            fields = repo.list_fields(parsed_id)
    return {"items": [{key: str(value) if isinstance(value, UUID) else value for key, value in item.items()}
                       for item in fields]}


@router.post("/{document_id}/fields/{field_id}/corrections")
def create_data_correction(document_id: str, field_id: str, payload: DataCorrectionRequest, request: Request,
                           identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    _require_processor(identity)
    try:
        parsed_document, parsed_field = UUID(document_id), UUID(field_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested field was not found.") from None
    typed = _typed_correction(payload.corrected_value)
    value = typed.canonical()
    try:
        with request.app.state.engine.connect() as connection:
            with api_tenant_transaction(connection, identity):
                repo = DocumentRepository(connection, identity.tenant_id)
                if repo.get(parsed_document) is None:
                    raise APIError(404, "not_found", "The requested document was not found.")
                correction_id = repo.append_data_correction(document_id=parsed_document, field_id=parsed_field,
                    value=value, reason=payload.reason, actor_id=identity.subject)
                saved = next(item for item in repo.list_data_corrections(parsed_document)
                             if str(item["id"]) == str(correction_id))
    except (SQLAlchemyError, RuntimeError, PermissionError):
        raise APIError(503, "correction_persistence_unavailable", "The correction could not be recorded.") from None
    except ValueError as error:
        if str(error) == "field_not_found_for_document":
            raise APIError(404, "not_found", "The requested field was not found.") from None
        raise APIError(422, "invalid_correction", "The correction could not be validated.") from None
    return jsonable_encoder(saved)


@router.get("/{document_id}/corrections/export")
def export_data_corrections(document_id: str, request: Request,
                            identity: AuthenticatedIdentity = Depends(require_identity)) -> Response:
    try:
        parsed_id = UUID(document_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document was not found.") from None
    with request.app.state.engine.connect() as connection:
        with api_tenant_transaction(connection, identity):
            repo = DocumentRepository(connection, identity.tenant_id)
            if repo.get(parsed_id) is None:
                raise APIError(404, "not_found", "The requested document was not found.")
            items = repo.list_data_corrections(parsed_id)
    body = json.dumps(jsonable_encoder(items), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return Response(body, media_type="application/json", headers={"Content-Disposition": f"attachment; filename=corrections-{parsed_id}.json",
                                                                    "X-Content-Type-Options": "nosniff"})


@router.put("/{document_id}/category")
def classify_document(document_id: str, payload: CategoryRequest, request: Request,
                      identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    if identity.role != "administrator":
        raise APIError(403, "administrator_required", "Only an administrator can authorize a document category.")
    try:
        parsed_id = UUID(document_id)
        with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
            DocumentRepository(connection, identity.tenant_id).set_document_category(
                parsed_id, category=payload.category, actor_id=identity.subject)
    except ValueError as error:
        if str(error) == "document_missing_or_already_has_edit_sets":
            raise APIError(409, "category_locked_or_document_missing", "The category cannot be changed after edits begin.") from None
        raise APIError(404, "not_found", "The requested document was not found.") from None
    except (SQLAlchemyError, RuntimeError, PermissionError):
        raise APIError(503, "classification_unavailable", "The document category could not be recorded.") from None
    return {"document_id": str(parsed_id), "category": payload.category, "classified_by": identity.subject}


@router.post("/{document_id}/edit-sets")
def create_edit_set_draft(document_id: str, payload: EditSetDraftRequest, request: Request,
                          identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    _require_processor(identity)
    try:
        parsed_id = UUID(document_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document was not found.") from None
    edit_set_id = uuid4()
    try:
        with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
            repo = DocumentRepository(connection, identity.tenant_id)
            document = repo.get(parsed_id)
            if document is None:
                raise APIError(404, "not_found", "The requested document was not found.")
            if document["document_category"] != "insurer-authored":
                raise APIError(403, "document_category_not_authorized", "Only an administrator-classified insurer-authored document can be changed.")
            if document["ingestion_state"] != "ready" or document["current_version_id"] is None:
                raise APIError(409, "document_not_ready", "Safe inspection must finish before creating edits.")
            repo.create_edit_set(edit_set_id=edit_set_id, document_id=parsed_id,
                base_version_id=UUID(str(document["current_version_id"])), category=str(document["document_category"]),
                actor_id=identity.subject, reason=payload.reason)
    except (SQLAlchemyError, RuntimeError, PermissionError):
        raise APIError(503, "edit_set_unavailable", "The edit draft could not be created.") from None
    except ValueError as error:
        raise APIError(409, str(error), "The document version or authorization changed.") from None
    return {"edit_set_id": str(edit_set_id), "document_id": str(parsed_id),
            "status": "draft", "base_version_id": str(document["current_version_id"]),
            "authorized_category": document["document_category"], "reason": payload.reason.strip()}


@router.get("/{document_id}/edit-sets")
def list_edit_sets(document_id: str, request: Request,
                   identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    try:
        parsed_id = UUID(document_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document was not found.") from None
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        if repo.get(parsed_id) is None:
            raise APIError(404, "not_found", "The requested document was not found.")
        items = repo.list_edit_sets(parsed_id)
        for item in items:
            item["patches"] = repo.list_patches(UUID(str(item["edit_set_id"])))
    return {"items": jsonable_encoder(items)}


@router.get("/{document_id}/audit")
def list_document_audit(document_id: str, request: Request,
                        identity: AuthenticatedIdentity = Depends(require_identity),
                        limit: int = Query(default=100, ge=1, le=500)) -> dict[str, object]:
    try:
        parsed_id = UUID(document_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document was not found.") from None
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        if repo.get(parsed_id) is None:
            raise APIError(404, "not_found", "The requested document was not found.")
        events = repo.list_document_audit(parsed_id, limit=limit)
    return {"items": jsonable_encoder(events)}


@router.put("/{document_id}/edit-sets/{edit_set_id}/patches")
def add_edit_set_patches(document_id: str, edit_set_id: str, payload: PatchBatchRequest, request: Request,
                         identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    _require_processor(identity)
    try:
        parsed_id, parsed_edit_id = UUID(document_id), UUID(edit_set_id)
    except ValueError:
        raise APIError(404, "not_found", "The edit draft was not found.") from None
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        edit_set = repo.get_edit_set(parsed_edit_id)
        if edit_set is None or edit_set["document_id"] != parsed_id:
            raise APIError(404, "not_found", "The edit draft was not found.")
        if edit_set["status"] != "draft" or edit_set["created_by"] != identity.subject:
            raise APIError(409, "edit_set_not_editable", "Only the author can change an open draft.")
        base_version_id = UUID(str(edit_set["base_version_id"]))
        if str(edit_set["current_version_id"]) != str(base_version_id):
            raise APIError(409, "stale_base_version", "The document changed; rebase the draft before continuing.")
    version, source = _source_version(request, identity, parsed_id, base_version_id)
    try:
        report = request.app.state.restricted_parser.inspect(
            request.app.state.document_storage.root / str(version["storage_key"]),
            filename=str(version.get("filename", "")) or str(edit_set["filename"]), include_targets=True)
    except ParserNotVerified:
        raise APIError(503, "preflight_not_verified", "Restricted target resolution is unavailable.") from None
    if report.sha256 != version["sha256"]:
        raise APIError(409, "stale_base_version", "The selected source version failed integrity validation.")
    targets = {item["id"]: item for item in report.targets}
    planned: list[dict[str, object]] = []
    for item in payload.patches:
        target = targets.get(item.target_id)
        if target is None or target.get("editable") is not True or target.get("fingerprint") != item.fingerprint:
            raise APIError(409, "target_not_editable_or_stale", "A patch target is unsupported or stale.")
        try:
            new_value = _typed_correction(item.new_value)
        except APIError:
            raise
        planned.append({"target_id": item.target_id, "expected_old_value": target["value"],
                        "expected_fingerprint": item.fingerprint, "new_value": new_value.canonical()})
    try:
        with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
            repo = DocumentRepository(connection, identity.tenant_id)
            current = repo.get_edit_set(parsed_edit_id)
            if current is None or current["status"] != "draft" or current["created_by"] != identity.subject:
                raise APIError(409, "edit_set_not_editable", "The edit draft changed while targets were being resolved.")
            if str(current["current_version_id"]) != str(base_version_id):
                raise APIError(409, "stale_base_version", "The document changed while targets were being resolved.")
            repo.add_patches(parsed_edit_id, planned, actor_id=identity.subject)
    except APIError:
        raise
    except (SQLAlchemyError, RuntimeError, PermissionError, ValueError):
        raise APIError(409, "patches_not_saved", "The patch set conflicted with the current draft.") from None
    return {"edit_set_id": str(parsed_edit_id), "status": "draft",
            "patches": [{"target_id": item["target_id"], "expected_old_value": item["expected_old_value"],
                         "new_value": item["new_value"]} for item in planned]}


@router.post("/{document_id}/edit-sets/{edit_set_id}/preflight")
def preflight_edit_set(document_id: str, edit_set_id: str, request: Request,
                       identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    _require_processor(identity)
    try:
        parsed_id, parsed_edit_id = UUID(document_id), UUID(edit_set_id)
    except ValueError:
        raise APIError(404, "not_found", "The edit draft was not found.") from None
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        edit_set = repo.get_edit_set(parsed_edit_id)
        if edit_set is None or edit_set["document_id"] != parsed_id:
            raise APIError(404, "not_found", "The edit draft was not found.")
        if edit_set["status"] != "draft" or edit_set["created_by"] != identity.subject:
            raise APIError(409, "edit_set_not_editable", "Only an open draft can be preflighted by its author.")
        if edit_set["authorized_category"] != "insurer-authored" or edit_set["document_category"] != "insurer-authored":
            raise APIError(403, "document_category_not_authorized", "The document is not authorized for native correction.")
        base_version_id = UUID(str(edit_set["base_version_id"]))
        if str(edit_set["current_version_id"]) != str(base_version_id):
            raise APIError(409, "stale_base_version", "Rebase the draft against the current version first.")
        stored_patches = repo.list_patches(parsed_edit_id)
    if not stored_patches:
        raise APIError(422, "patch_set_empty", "Add at least one validated target before preflight.")
    version, source = _source_version(request, identity, parsed_id, base_version_id)
    try:
        report = request.app.state.restricted_parser.inspect(
            request.app.state.document_storage.root / str(version["storage_key"]),
            filename=str(edit_set["filename"]), include_targets=True)
    except ParserNotVerified:
        raise APIError(503, "preflight_not_verified", "Restricted preflight is unavailable.") from None
    if report.sha256 != version["sha256"]:
        raise APIError(409, "stale_base_version", "The base version failed its source fingerprint check.")
    workflow = _release_workflow(str(report.adapter))
    target_map = {item["id"]: item for item in report.targets}
    patch_values: list[Patch] = []
    for item in stored_patches:
        target = target_map.get(str(item["target_id"]))
        if target is None or target.get("editable") is not True or target.get("fingerprint") != str(item["expected_fingerprint"]).strip():
            raise APIError(409, "target_not_editable_or_stale", "A persisted patch no longer resolves to its authorized target.")
        patch_values.append(Patch(
            # PatchPlan's source_version_id is the immutable byte fingerprint used by
            # the core planner; FrozenEditSet separately binds the registry UUID.
            source_version_id=str(version["sha256"]), target_id=str(item["target_id"]),
            expected_old_value=_typed_correction(item["expected_old_value"], allow_metadata=True),
            expected_fingerprint=str(item["expected_fingerprint"]).strip(),
            new_value=_typed_correction(item["new_value"], allow_metadata=True),
        ))
    try:
        frozen = workflow.freeze(source=source, document_id=str(parsed_id), base_version_id=str(base_version_id),
            actor_id=identity.subject, role=identity.role, authorized_category="insurer-authored",
            reason=str(edit_set["reason"]), patches=patch_values, edit_set_id=str(parsed_edit_id))
        if report.adapter == "docx":
            # DOCX is not reviewable until its critical isolated render/layout
            # verifier has completed. Keep the edit as a draft on hosts where
            # that boundary is unavailable; never queue an unreleasable plan.
            readiness = workflow.release(source=source, edit_set=frozen)
            if readiness.status != "VERIFIED":
                raise APIError(422, "critical_docx_verification_unavailable",
                               "DOCX preflight requires a verified isolated render and layout check.")
        plan_payload = serialize_frozen_edit_set(frozen)
    except APIError:
        raise
    except (PlanError, WorkflowRejected, ValueError) as error:
        reason = getattr(error, "code", getattr(error, "reason_code", str(error)))
        raise APIError(422, str(reason), "The edit set failed preflight validation.") from None
    try:
        with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
            repo = DocumentRepository(connection, identity.tenant_id)
            current = repo.get_edit_set(parsed_edit_id)
            if current is None or current["status"] != "draft" or str(current["current_version_id"]) != str(base_version_id):
                raise APIError(409, "stale_base_version", "The edit draft changed during preflight.")
            repo.freeze_edit_set(parsed_edit_id, frozen_hash=frozen.frozen_sha256,
                                 plan=plan_payload, adapter=str(report.adapter),
                                 adapter_version=str(report.adapter_version), actor_id=identity.subject)
    except APIError:
        raise
    except (SQLAlchemyError, RuntimeError, PermissionError, ValueError):
        raise APIError(409, "freeze_conflict", "The plan could not be frozen because the draft changed.") from None
    return {"edit_set_id": str(parsed_edit_id), "status": "submitted", "frozen_hash": frozen.frozen_sha256,
            "plan_sha256": frozen.plan.plan_sha256,
            "changes": [{"target_id": patch.target_id, "old": patch.expected_old_value.canonical(),
                         "proposed": patch.new_value.canonical()} for patch in frozen.plan.patches],
            "dependents": list(frozen.plan.dependents), "dependencies_unknown": frozen.plan.dependencies_unknown,
            "approval_required": True}


@router.post("/{document_id}/edit-sets/{edit_set_id}/rebase")
def rebase_edit_set(document_id: str, edit_set_id: str, request: Request,
                    identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    _require_processor(identity)
    try:
        parsed_id, parsed_edit_id = UUID(document_id), UUID(edit_set_id)
    except ValueError:
        raise APIError(404, "not_found", "The edit draft was not found.") from None
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        draft = repo.get_edit_set(parsed_edit_id)
        if draft is None or draft["document_id"] != parsed_id:
            raise APIError(404, "not_found", "The edit draft was not found.")
        if draft["status"] != "draft" or draft["created_by"] != identity.subject:
            raise APIError(409, "edit_set_not_rebasable", "Only the author may rebase an unfrozen draft.")
        patches = repo.list_patches(parsed_edit_id)
        document = repo.get(parsed_id)
        if document is None or document["current_version_id"] is None:
            raise APIError(404, "not_found", "The requested document was not found.")
        new_version_id = UUID(str(document["current_version_id"]))
        if new_version_id == UUID(str(draft["base_version_id"])):
            raise APIError(409, "rebase_not_required", "The document has not changed since this draft was created.")
        if document["document_category"] != "insurer-authored":
            raise APIError(403, "document_category_not_authorized", "The current document classification does not permit editing.")
    version, source = _source_version(request, identity, parsed_id, new_version_id)
    try:
        report = request.app.state.restricted_parser.inspect(
            request.app.state.document_storage.root / str(version["storage_key"]),
            filename=str(draft["filename"]), include_targets=True)
    except ParserNotVerified:
        raise APIError(503, "rebase_not_verified", "Restricted target resolution is unavailable.") from None
    if report.sha256 != version["sha256"]:
        raise APIError(409, "stale_base_version", "The proposed rebase version failed its source fingerprint check.")
    if report.status != "inspected" or report.adapter not in {"csv", "json", "xlsx", "pdf", "docx"}:
        raise APIError(415, "native_adapter_unsupported", "This version has no safe native editing adapter.")
    target_map = {item["id"]: item for item in report.targets}
    rebased: list[dict[str, object]] = []
    conflicts: list[dict[str, str]] = []
    for patch in patches:
        target = target_map.get(str(patch["target_id"]))
        expected = patch["expected_old_value"]
        if (target is None or target.get("editable") is not True
                or target["value"].get("type") != expected.get("type")
                or target["value"].get("value") != expected.get("value")):
            conflicts.append({"target_id": str(patch["target_id"]), "reason": "base_value_changed_or_target_missing"})
            continue
        rebased.append({"target_id": str(patch["target_id"]), "expected_old_value": target["value"],
                        "expected_fingerprint": target["fingerprint"], "new_value": patch["new_value"]})
    if conflicts:
        raise APIError(409, "rebase_conflicts", "The new document version changed one or more target values.",
                       details=conflicts)
    new_edit_set_id = uuid4()
    try:
        with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
            repo = DocumentRepository(connection, identity.tenant_id)
            current = repo.get(parsed_id)
            if current is None or str(current["current_version_id"]) != str(new_version_id):
                raise APIError(409, "stale_base_version", "The document changed during rebase.")
            repo.create_edit_set(edit_set_id=new_edit_set_id, document_id=parsed_id,
                base_version_id=new_version_id, category=str(current["document_category"]),
                actor_id=identity.subject, reason=str(draft["reason"]))
            repo.add_patches(new_edit_set_id, rebased, actor_id=identity.subject)
    except APIError:
        raise
    except (SQLAlchemyError, RuntimeError, PermissionError, ValueError):
        raise APIError(409, "rebase_conflict", "The draft could not be rebased against the current version.") from None
    return {"previous_edit_set_id": str(parsed_edit_id), "edit_set_id": str(new_edit_set_id),
            "base_version_id": str(new_version_id), "status": "draft", "patches": len(rebased)}


@router.post("/{document_id}/edit-sets/{edit_set_id}/review")
def review_edit_set(document_id: str, edit_set_id: str, payload: ReviewRequest, request: Request,
                    identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    if identity.role not in {"reviewer", "administrator"}:
        raise APIError(403, "reviewer_role_required", "Only a reviewer or administrator may decide this edit set.")
    try:
        parsed_id, parsed_edit_id = UUID(document_id), UUID(edit_set_id)
    except ValueError:
        raise APIError(404, "not_found", "The edit set was not found.") from None
    try:
        with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
            repo = DocumentRepository(connection, identity.tenant_id)
            edit_set = repo.get_edit_set(parsed_edit_id)
            if edit_set is None or edit_set["document_id"] != parsed_id:
                raise APIError(404, "not_found", "The edit set was not found.")
            repo.review_edit_set(parsed_edit_id, decision=payload.decision, reviewer_id=identity.subject,
                                 frozen_hash=str(edit_set["frozen_hash"]).strip(), note=payload.note)
    except APIError:
        raise
    except ValueError as error:
        code = "reviewer_must_differ_from_author" if str(error) == "reviewer_must_differ_from_author" else "review_precondition_failed"
        raise APIError(409, code, "This edit set cannot be reviewed in its current state.") from None
    except (SQLAlchemyError, RuntimeError, PermissionError):
        raise APIError(503, "review_unavailable", "The review decision could not be recorded.") from None
    return {"edit_set_id": str(parsed_edit_id), "status": payload.decision, "reviewer_id": identity.subject}


@router.post("/{document_id}/edit-sets/{edit_set_id}/release")
def release_edit_set(document_id: str, edit_set_id: str, request: Request,
                     identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Apply an approved frozen plan, independently verify it, scan it, then atomically register it."""
    if identity.role not in {"processor", "administrator"}:
        raise APIError(403, "role_not_permitted", "A processor role is required to release this correction.")
    try:
        parsed_document, parsed_edit_set = UUID(document_id), UUID(edit_set_id)
    except ValueError:
        raise APIError(404, "not_found", "The edit set was not found.") from None
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        edit_set = repo.get_edit_set(parsed_edit_set)
        if edit_set is None or edit_set["document_id"] != parsed_document:
            raise APIError(404, "not_found", "The edit set was not found.")
        if edit_set["status"] == "released" and edit_set["released_version_id"] is not None:
            versions = repo.list_versions(parsed_document)
            released = next((v for v in versions if str(v["version_id"]) == str(edit_set["released_version_id"])), None)
            return {"edit_set_id": str(parsed_edit_set), "version_id": str(edit_set["released_version_id"]),
                    "outcome": (released or {}).get("verification_outcome", "INCOMPLETE"), "idempotent": True}
        if edit_set["status"] != "approved":
            raise APIError(409, "approval_required", "A reviewer must approve the frozen edit set before release.")
        if str(edit_set["current_version_id"]) != str(edit_set["base_version_id"]):
            raise APIError(409, "stale_base_version", "The document changed after this edit set was created.")
        frozen_payload = edit_set.get("patch_plan")
        adapter_name = str(edit_set.get("adapter") or "")
        adapter_version = str(edit_set.get("adapter_version") or "unknown")
    try:
        frozen = deserialize_frozen_edit_set(frozen_payload)
    except (ValueError, KeyError, TypeError):
        raise APIError(409, "frozen_plan_integrity_failed", "The stored frozen plan failed its integrity check.") from None
    if frozen.edit_set_id != str(parsed_edit_set) or frozen.document_id != str(parsed_document):
        raise APIError(409, "frozen_plan_identity_mismatch", "The frozen plan is bound to a different edit set.")
    version, source = _source_version(request, identity, parsed_document, UUID(str(edit_set["base_version_id"])))
    workflow = _release_workflow(adapter_name)
    result = workflow.release(source=source, edit_set=frozen, allow_incomplete=True)
    base_manifest: dict[str, object] = {
        "source_sha256": str(version["sha256"]), "adapter": adapter_name,
        "adapter_version": adapter_version, "edit_set_id": str(parsed_edit_set),
        "plan_sha256": frozen.plan.plan_sha256,
    }
    if result.verification is not None:
        base_manifest["verification"] = result.verification.as_dict()
    outcome = result.status
    staged = None
    stored = None
    if outcome in {"VERIFIED", "INCOMPLETE"} and result.derived_bytes is not None:
        storage: ImmutableLocalStorage = request.app.state.document_storage
        staged = storage.stage(identity.tenant_id, parsed_document, uuid4(), io.BytesIO(result.derived_bytes),
                               max_bytes=request.app.state.settings.max_upload_bytes)
        try:
            scan: ScanResult = request.app.state.ingestion_pipeline.scanner.scan(staged.path)
        except Exception:
            scan = ScanResult("error", staged.sha256, "scanner", reason_code="scanner_failure")
        base_manifest["malware_scan"] = {"status": scan.status, "sha256": scan.sha256,
                                          "scanner": scan.scanner, "reason_code": scan.reason_code}
        if scan.status == "infected":
            storage.quarantine(staged)
            staged = None
            outcome = "BLOCKED"
        elif scan.status != "clean" or scan.sha256 != staged.sha256:
            storage.discard_staged(staged)
            staged = None
            outcome = "BLOCKED"
        else:
            stored = storage.promote_derived(staged, scan, edit_set_id=parsed_edit_set)
            staged = None
    else:
        outcome = "BLOCKED" if outcome == "BLOCKED" else outcome
    if result.status == "NO_CHANGE":
        raise APIError(409, "no_change", "The proposed edit does not change the source document.")
    if outcome == "BLOCKED" or stored is None:
        if result.status == "INCOMPLETE" and result.derived_bytes is None:
            base_manifest["limitation"] = "verifier_did_not_release_candidate_bytes"
        with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
            DocumentRepository(connection, identity.tenant_id).record_blocked_release(
                edit_set_id=parsed_edit_set, outcome="BLOCKED", adapter=adapter_name,
                adapter_version=adapter_version, source_sha256=str(version["sha256"]),
                manifest={**base_manifest, "outcome": "BLOCKED"}, actor_id=identity.subject)
        return {"edit_set_id": str(parsed_edit_set), "outcome": "BLOCKED", "version_id": None,
                "download_available": False, "verification": result.verification.as_dict() if result.verification else None}
    base_manifest["outcome"] = outcome
    base_manifest["media_type"] = str(version.get("mime") or "application/octet-stream")
    try:
        with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
            new_version = DocumentRepository(connection, identity.tenant_id).commit_derived_release(
                edit_set_id=parsed_edit_set, stored=stored, outcome=outcome,
                adapter=adapter_name, adapter_version=adapter_version,
                manifest=base_manifest, actor_id=identity.subject)
    except (SQLAlchemyError, RuntimeError, PermissionError, ValueError):
        # The content-addressed object remains private and is harmlessly reusable on retry.
        raise APIError(409, "release_commit_failed", "The verified candidate was not registered; retry after checking the document version.") from None
    return {"edit_set_id": str(parsed_edit_set), "outcome": outcome, "version_id": str(new_version),
            "download_available": outcome == "VERIFIED", "verification": result.verification.as_dict() if result.verification else None}


@router.get("/{document_id}/versions/{version_id}/download")
def download_document_version(document_id: str, version_id: str, request: Request,
                              identity: AuthenticatedIdentity = Depends(require_identity)) -> Response:
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
        if version["kind"] == "derived":
            versions = repo.list_versions(parsed_document)
            item = next((entry for entry in versions if str(entry["version_id"]) == str(parsed_version)), None)
            if item is None or item.get("verification_outcome") != "VERIFIED":
                raise APIError(409, "verified_download_required", "Only independently verified derived files may be downloaded.")
        stored = StoredFile(identity.tenant_id, parsed_document, storage.root / str(version["storage_key"]),
                            str(version["storage_key"]), int(version["size_bytes"]), str(version["sha256"]))
        content = storage.read_original(stored)
        filename = _filename(str(version["filename"]))
        media_type = str(version.get("mime") or "application/octet-stream")
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
                             "X-Content-Type-Options": "nosniff"})


@router.get("/{document_id}/corrections")
def list_data_corrections(document_id: str, request: Request,
                          identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    try:
        parsed_id = UUID(document_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document was not found.") from None
    with request.app.state.engine.connect() as connection:
        with api_tenant_transaction(connection, identity):
            repo = DocumentRepository(connection, identity.tenant_id)
            if repo.get(parsed_id) is None:
                raise APIError(404, "not_found", "The requested document was not found.")
            items = repo.list_data_corrections(parsed_id)
    return {"items": jsonable_encoder(items)}
