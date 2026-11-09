from __future__ import annotations

import hashlib
import tempfile
import io
import json
import time
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
from packages.registry.documents import DocumentRepository, is_download_eligible
from packages.storage.local import ImmutableLocalStorage, StoredFile
from packages.workers.parser import ParserNotVerified, PdfVerificationWorkerUnavailable, RestrictedPdfVerifier
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
from packages.workflow.release_lock import ReleaseInProgress, claim_release
from packages.ingestion.models import ScanResult
from packages.core.planning.planner import PlanError

router = APIRouter(prefix="/api/documents", tags=["documents"])
MAX_VIEW_TARGET_OFFSET = 10_000_000
MAX_TEXT_ANCHOR_CHARS = 500
TEXT_ANCHOR_CONTEXT_CHARS = 40
MAX_VIEW_TARGET_LOOKUP = 50


class TextAnchorRequest(BaseModel):
    """A user selection inside text the server extracted from one immutable source version."""
    model_config = ConfigDict(extra="forbid")
    source_version_id: UUID
    kind: str = Field(pattern=r"^(pdf_page|docx_block)$")
    page: int | None = Field(default=None, ge=1, le=100_000)
    block_id: str | None = Field(default=None, min_length=1, max_length=300)
    start: int = Field(ge=0, le=1_000_000)
    end: int = Field(ge=1, le=1_000_000)
    text: str = Field(min_length=1, max_length=MAX_TEXT_ANCHOR_CHARS)


class ManualFieldRequest(BaseModel):
    """Exactly one source: a parser-discovered target (editable or not) or a validated text anchor."""
    model_config = ConfigDict(extra="forbid")
    target_id: str | None = Field(default=None, min_length=1, max_length=500)
    fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    text_anchor: TextAnchorRequest | None = None
    label: str | None = Field(default=None, min_length=1, max_length=255)


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


def _require_owner(identity: AuthenticatedIdentity) -> None:
    if identity.role != "OWNER":
        raise APIError(403, "owner_required", "An authenticated owner identity is required.")


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
    return dict(getattr(report, "capability"))


VIEWABLE_ADAPTERS = frozenset({"csv", "json", "xlsx", "pdf", "docx"})


def _capabilities(report: Any) -> dict[str, dict[str, object]]:
    """Three independent capabilities: viewable, natively editable, eligible for sidecar corrections."""
    capability = dict(report.capability or {})
    reasons = list(capability.get("reasons") or [])
    level = capability.get("level")
    if level == "rejected":
        viewable, view_reason = False, reasons[0] if reasons else "document_rejected"
    elif report.status != "inspected" or report.adapter not in VIEWABLE_ADAPTERS:
        viewable, view_reason = False, reasons[0] if reasons else "viewer_not_supported"
    else:
        viewable, view_reason = True, None
    native = viewable and level == "editable"
    details = report.details or {}
    if not viewable:
        anchorable, anchor_reason = False, view_reason
    elif report.adapter == "pdf":
        anchorable = bool(details.get("fields")) or any(
            not page.get("scanned_or_image_only") and str(page.get("text", "")).strip() for page in details.get("pages", []))
        anchor_reason = None if anchorable else "no_extractable_text_or_fields"
    elif report.adapter == "docx":
        anchorable = bool(details.get("blocks"))
        anchor_reason = None if anchorable else "no_extractable_text"
    else:
        page = report.targets_page or {}
        total = page.get("total") if page.get("total") is not None else len(report.targets or ())
        anchorable = bool(total)
        anchor_reason = None if anchorable else "no_values_found"
    return {"viewable": {"available": viewable, "reason": view_reason},
            "native_editing": {"available": native, "reason": None if native else (view_reason if not viewable else reasons[0] if reasons else "document_not_editable")},
            "data_corrections": {"available": anchorable, "reason": anchor_reason}}


def _release_workflow(adapter_name: str, *, docx_mode: str = "structural", pdf_verifier=None) -> NativeReleaseWorkflow:
    if adapter_name == "csv":
        return NativeReleaseWorkflow(CsvAdapter(), CsvIndependentVerifier())
    if adapter_name == "json":
        return NativeReleaseWorkflow(JsonAdapter(), JsonIndependentVerifier())
    if adapter_name == "xlsx":
        return XlsxReleaseWorkflow()
    if adapter_name == "pdf":
        return NativeReleaseWorkflow(PdfAdapter(), pdf_verifier or PdfIndependentVerifier())
    if adapter_name == "docx":
        return NativeReleaseWorkflow(DocxAdapter(), DocxIndependentVerifier(mode=docx_mode))
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
        "inspection_score": report.score, "state": state,
        # Ready for viewing only when a safe viewer exists for this file (a legacy .doc is inspected
        # but has no viewer, so it is never reported as viewable).
        "viewing_ready": state == "ready" and _capabilities(report)["viewable"]["available"],
        "capabilities": _capabilities(report),
        "targets": list(report.targets), "targets_truncated": report.targets_truncated,
        "targets_page": report.targets_page, "editable_target_count": getattr(report, "targets_editable_total", None),
        "details": report.details,
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
                          target_offset: int = Query(default=0, ge=0, le=MAX_VIEW_TARGET_OFFSET),
                          target_id: list[str] = Query(default=[]),
                          identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Return one bounded page of native targets, or an exact lookup of up to MAX_VIEW_TARGET_LOOKUP ids."""
    if len(target_id) > MAX_VIEW_TARGET_LOOKUP or any(not item or len(item) > 512 for item in target_id):
        raise APIError(422, "target_lookup_invalid", "Request at most 50 exact target identifiers.")
    if target_id and target_offset:
        raise APIError(422, "target_lookup_invalid", "Use either a target page or an exact target lookup.")
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
        report = request.app.state.restricted_parser.inspect(
            stored.path, filename=filename, include_targets=True, target_offset=target_offset,
            target_ids=target_id or None)
    except ParserNotVerified:
        raise APIError(503, "view_not_verified", "Safe document viewing is unavailable.") from None
    if report.sha256 != row["sha256"]:
        raise APIError(409, "source_version_changed", "The requested version failed integrity validation.")
    if report.adapter not in VIEWABLE_ADAPTERS or report.status != "inspected":
        raise APIError(415, "viewer_not_supported", "This file type has no supported read-only viewer.",
                       details=[{"capability": _presentation_capability(report), "capabilities": _capabilities(report)}])
    page = report.targets_page or {}
    if target_offset and page.get("total") is not None and target_offset >= int(page["total"]):
        raise APIError(416, "target_page_out_of_range", "The requested target page is beyond the document.")
    return {
        "document_id": str(parsed_id), "version_id": str(parsed_version_id),
        "parent_version_id": str(row["parent_version_id"]) if row["parent_version_id"] else None,
        "version_kind": row["kind"], "filename": filename, "source_sha256": report.sha256,
        "size_bytes": len(source), "media_type": report.media_type,
        "capability": _presentation_capability(report), "adapter": report.adapter,
        "docx_verification_mode": request.app.state.settings.channel360_docx_verification_mode
        if report.adapter == "docx" else None,
        "features": list(report.features), "targets": list(report.targets),
        "capabilities": _capabilities(report),
        "targets_truncated": report.targets_truncated, "targets_page": report.targets_page,
        "editable_target_count": getattr(report, "targets_editable_total", None),
        "details": report.details,
        "structural_summary": ({**report.structural_summary, "source_document_id": str(parsed_id),
                                "source_version_id": str(parsed_version_id)} if report.structural_summary else None),
    }


@router.post("/{document_id}/versions/{version_id}/ai-summary")
def generate_ai_summary(document_id: str, version_id: str, request: Request,
                        identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Generate an optional explanation from bounded evidence for one authorized immutable version."""
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
                raise APIError(409, "document_not_ready", "The document must finish safe inspection first.")
            if row["kind"] == "derived":
                version = next((item for item in repo.list_versions(parsed_id)
                                if str(item["version_id"]) == str(parsed_version_id)), None)
                if version is None or version.get("verification_outcome") != "VERIFIED":
                    raise APIError(404, "not_found", "The requested version was not found.")
            stored = StoredFile(identity.tenant_id, parsed_id, storage.root / str(row["storage_key"]),
                                str(row["storage_key"]), int(row["size_bytes"]), str(row["sha256"]))
            storage.read_original(stored)
            filename = _filename(str(row["filename"]))
    try:
        report = request.app.state.restricted_parser.inspect(
            stored.path, filename=filename, include_summary_evidence=True, timeout_seconds=30)
    except ParserNotVerified:
        raise APIError(503, "view_not_verified", "Safe document inspection is unavailable.") from None
    if report.status != "inspected" or report.adapter not in {"csv", "json", "xlsx", "pdf", "docx"}:
        raise APIError(415, "viewer_not_supported", "This file type has no supported safe text extraction.")
    if report.sha256 != row["sha256"]:
        raise APIError(409, "source_version_changed", "The requested version failed integrity validation.")
    if report.targets_truncated:
        raise APIError(413, "summary_evidence_limit", "The document exceeds safe summary evidence limits.")
    evidence = list(report.summary_evidence)
    pdf_pages = (report.details or {}).get("pages", []) if report.adapter == "pdf" else []
    if not evidence or (pdf_pages and not any(not page.get("scanned_or_image_only") for page in pdf_pages)):
        raise APIError(422, "ai_insufficient_readable_content",
                       "No readable document content is available to explain. Scanned PDFs require OCR, which is not enabled.")
    provider = getattr(request.app.state, "llm_provider", None)
    if provider is None:
        from packages.core.summaries.llm import LLMConfigurationError, OpenAICompatibleExplanationProvider
        try:
            provider = OpenAICompatibleExplanationProvider(request.app.state.settings)
        except LLMConfigurationError:
            raise APIError(503, "ai_not_configured", "AI explanations are unavailable until a server-side provider is configured.") from None
    now = time.monotonic()
    limiter = request.app.state.ai_summary_requests
    with request.app.state.ai_summary_lock:
        recent = [stamp for stamp in limiter.get((str(identity.tenant_id), identity.subject), []) if now - stamp < 60]
        if len(recent) >= 5:
            raise APIError(429, "ai_rate_limited", "The AI explanation limit is five requests per user per minute.")
        recent.append(now)
        limiter[(str(identity.tenant_id), identity.subject)] = recent
    from packages.core.summaries.llm import LLMProviderError
    try:
        result = provider.explain(source_document_id=str(parsed_id), source_version_id=str(parsed_version_id),
                                 source_sha256=report.sha256,
                                 structural_summary=report.structural_summary or {}, evidence=evidence)
    except LLMProviderError as error:
        messages = {
            "provider_timeout": ("The AI provider did not respond in time.", 504),
            "provider_rate_limited": ("The AI provider is rate limiting requests. Try again later.", 429),
            "provider_credentials_rejected": ("The configured AI provider credentials were rejected.", 503),
            "provider_unavailable": ("The AI provider is temporarily unavailable.", 503),
            "provider_response_not_grounded": ("The AI response did not pass source-reference and text-overlap checks. Those checks do not verify factual accuracy.", 502),
            "insufficient_readable_evidence": ("There is not enough readable content to explain.", 422),
        }
        message, status = messages.get(error.code, ("The AI provider returned an invalid response.", error.status_code))
        raise APIError(status, error.code, message) from None
    except Exception:
        # Provider exceptions may contain request/response material; never propagate or log their text.
        raise APIError(502, "provider_request_failed", "The AI explanation could not be generated.") from None
    if (result.get("source_document_id") != str(parsed_id) or
            result.get("source_version_id") != str(parsed_version_id) or
            result.get("source_sha256") != report.sha256 or result.get("generation_method") != "ai"):
        raise APIError(502, "provider_response_invalid", "The AI response did not match the selected source version.")
    return result


@router.post("/{document_id}/fields")
def create_manual_field(document_id: str, payload: ManualFieldRequest, request: Request,
                        identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Register a sidecar data-correction field bound to safely inspected source content.

    A field is either a parser-discovered target (native editability is irrelevant: corrections never
    touch the file) or a text anchor that the server re-extracts and checks against the immutable
    source. The observed value always comes from the server's own extraction, never from the client.
    """
    _require_owner(identity)
    try:
        parsed_id = UUID(document_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested document was not found.") from None
    by_target = payload.target_id is not None or payload.fingerprint is not None
    if by_target == (payload.text_anchor is not None) or (by_target and (payload.target_id is None or payload.fingerprint is None)):
        raise APIError(422, "field_source_invalid",
                       "Provide either target_id and fingerprint, or one text_anchor.")
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
    anchor = payload.text_anchor
    if anchor is not None and anchor.source_version_id != version_id:
        raise APIError(409, "stale_source_version", "The selection was made on a version that is no longer current.")
    try:
        report = request.app.state.restricted_parser.inspect(
            stored.path, filename=filename, include_targets=anchor is None,
            target_ids=[payload.target_id] if anchor is None else None)
    except ParserNotVerified:
        raise APIError(503, "target_resolution_not_verified", "Safe target resolution is unavailable.") from None
    if report.sha256 != stored.sha256:
        raise APIError(409, "source_version_changed", "The source version changed during target resolution.")
    if report.status != "inspected" or report.adapter not in {"csv", "json", "xlsx", "pdf", "docx"}:
        raise APIError(409, "document_not_viewable", "This document has no safe viewer, so no field can be anchored in it.")
    base_evidence = {"source_sha256": report.sha256, "source_version_id": str(version_id), "adapter": report.adapter,
                     "adapter_version": report.adapter_version}
    if anchor is None:
        target = next((item for item in report.targets if item.get("id") == payload.target_id), None)
        if target is None or target.get("fingerprint") != payload.fingerprint:
            raise APIError(409, "target_not_found_or_stale", "The selected value is not present in the current source version.")
        target_id, fingerprint, observed = str(payload.target_id), str(payload.fingerprint), target["value"]
        label = payload.label or str(target.get("label") or payload.target_id)
        evidence = {**base_evidence, "kind": "native_target", "target_id": target_id,
                    "natively_editable": target.get("editable") is True, "native_reason": target.get("reason")}
    else:
        target_id, observed, label, evidence = _resolve_text_anchor(report, anchor, payload.label, base_evidence)
        fingerprint = hashlib.sha256(json.dumps(
            {"source_sha256": report.sha256, "anchor": target_id, "text": observed["value"]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        evidence["text_sha256"] = hashlib.sha256(str(observed["value"]).encode()).hexdigest()
    try:
        with request.app.state.engine.connect() as connection:
            with api_tenant_transaction(connection, identity):
                repo = DocumentRepository(connection, identity.tenant_id)
                current = repo.get(parsed_id)
                if current is None or str(current.get("current_version_id")) != str(version_id) or current.get("sha256") != stored.sha256:
                    raise APIError(409, "source_version_changed", "The source version changed during target resolution.")
                field_id = repo.create_manual_field(document_id=parsed_id, version_id=version_id,
                    target_id=target_id, fingerprint=fingerprint, label=label,
                    observed_value=observed, actor_id=identity.subject, evidence=[evidence])
    except APIError:
        raise
    except (SQLAlchemyError, RuntimeError, PermissionError, ValueError):
        raise APIError(503, "field_registration_unavailable", "The selected field could not be recorded.") from None
    return {"field_id": str(field_id), "document_id": str(parsed_id), "target_id": target_id,
            "label": label, "observed_value": observed, "source_version_id": str(version_id),
            "target_fingerprint": fingerprint, "evidence": [evidence]}


def _resolve_text_anchor(report: Any, anchor: TextAnchorRequest, label: str | None,
                         base_evidence: dict[str, Any]) -> tuple[str, dict[str, Any], str, dict[str, Any]]:
    """Validate a text selection against the text the restricted parser extracted from the source."""
    details = report.details or {}
    if anchor.end <= anchor.start or anchor.end - anchor.start > MAX_TEXT_ANCHOR_CHARS:
        raise APIError(422, "text_anchor_out_of_range", "The selection must cover 1 to 500 characters.")
    if anchor.kind == "pdf_page":
        if report.adapter != "pdf" or anchor.page is None or anchor.block_id is not None:
            raise APIError(422, "text_anchor_not_supported", "A PDF page anchor needs a PDF document and a page number.")
        page = next((item for item in details.get("pages", []) if item.get("page") == anchor.page), None)
        if page is None:
            raise APIError(422, "text_anchor_out_of_range", "The selected page does not exist in this source.")
        if page.get("scanned_or_image_only") or not str(page.get("text", "")).strip():
            raise APIError(422, "text_not_extractable",
                           "This page has no extractable text layer. OCR is not enabled, so no text can be anchored.")
        text = str(page["text"])
        target_id = f"text:pdf:page:{anchor.page}:{anchor.start}-{anchor.end}"
        location: dict[str, Any] = {"page": anchor.page}
        extraction = "pdfium_text_layer"
        default_label = f"Page {anchor.page} text"
    else:
        if report.adapter != "docx" or anchor.block_id is None or anchor.page is not None:
            raise APIError(422, "text_anchor_not_supported", "A Word anchor needs a DOCX document and a text block.")
        block = next((item for item in details.get("blocks", []) if item.get("id") == anchor.block_id), None)
        if block is None:
            raise APIError(422, "text_anchor_out_of_range", "The selected text block does not exist in this source.")
        text = str(block.get("text", ""))
        target_id = f"text:docx:{anchor.block_id}:{anchor.start}-{anchor.end}"
        location = {"block_id": anchor.block_id, "part": block.get("part"), "paragraph_index": block.get("paragraph_index"),
                    "block_kind": block.get("kind")}
        extraction = "docx_w_t_runs_excluding_deleted_text"
        default_label = f"{str(block.get('kind', 'paragraph')).replace('_', ' ').capitalize()} text"
    if anchor.end > len(text):
        raise APIError(422, "text_anchor_out_of_range", "The selection extends beyond the extracted source text.")
    selected = text[anchor.start:anchor.end]
    if selected != anchor.text:
        raise APIError(409, "text_selection_mismatch", "The selected text does not match the immutable source at that location.")
    if not selected.strip():
        raise APIError(422, "text_selection_empty", "Select visible text, not only whitespace.")
    observed = {"type": "string", "value": selected, "native": selected, "display": None}
    evidence = {**base_evidence, "kind": "text_selection", "anchor_kind": anchor.kind, "location": location,
                "start": anchor.start, "end": anchor.end, "extraction": extraction,
                "context_before": text[max(0, anchor.start - TEXT_ANCHOR_CONTEXT_CHARS):anchor.start],
                "context_after": text[anchor.end:anchor.end + TEXT_ANCHOR_CONTEXT_CHARS]}
    return target_id, observed, label or default_label, evidence


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
    _require_owner(identity)
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
    _require_owner(identity)
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
    _require_owner(identity)
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
                raise APIError(403, "document_category_not_authorized", "Only a document classified as insurer-authored can be changed.")
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
    _require_owner(identity)
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
            filename=str(version.get("filename", "")) or str(edit_set["filename"]), include_targets=True,
            target_ids=[item.target_id for item in payload.patches])
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
    _require_owner(identity)
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
            filename=str(edit_set["filename"]), include_targets=True,
            target_ids=[str(item["target_id"]) for item in stored_patches])
    except ParserNotVerified:
        raise APIError(503, "preflight_not_verified", "Restricted preflight is unavailable.") from None
    if report.sha256 != version["sha256"]:
        raise APIError(409, "stale_base_version", "The base version failed its source fingerprint check.")
    docx_mode = request.app.state.settings.channel360_docx_verification_mode
    workflow = _release_workflow(str(report.adapter), docx_mode=docx_mode)
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
            readiness = workflow.release(source=source, edit_set=frozen)
            expected = "STRUCTURALLY_VERIFIED" if docx_mode == "structural" else "VERIFIED"
            if readiness.status != expected:
                code = "docx_structural_preflight_failed" if docx_mode == "structural" else "critical_docx_visual_verification_unavailable"
                message = ("DOCX structural preflight failed." if docx_mode == "structural" else
                           "The configured strict DOCX profile requires successful independent visual verification.")
                raise APIError(422, code, message)
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
    return {"edit_set_id": str(parsed_edit_id), "status": "ready_for_release", "frozen_hash": frozen.frozen_sha256,
            "plan_sha256": frozen.plan.plan_sha256,
            "changes": [{"target_id": patch.target_id, "old": patch.expected_old_value.canonical(),
                         "proposed": patch.new_value.canonical()} for patch in frozen.plan.patches],
            "dependents": list(frozen.plan.dependents), "dependencies_unknown": frozen.plan.dependencies_unknown,
            "verification_profile": docx_mode if report.adapter == "docx" else None}


@router.post("/{document_id}/edit-sets/{edit_set_id}/rebase")
def rebase_edit_set(document_id: str, edit_set_id: str, request: Request,
                    identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Rebase a stale draft or frozen (ready_for_release) edit set onto the current version.

    A NEW draft is created; the predecessor keeps its frozen plan, hash and patches and becomes
    superseded only once the successor exists. Conflicts against the new source are reported, never
    resolved silently.
    """
    _require_owner(identity)
    try:
        parsed_id, parsed_edit_id = UUID(document_id), UUID(edit_set_id)
    except ValueError:
        raise APIError(404, "not_found", "The edit draft was not found.") from None
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        draft = repo.get_edit_set(parsed_edit_id)
        if draft is None or draft["document_id"] != parsed_id:
            raise APIError(404, "not_found", "The edit draft was not found.")
        if draft["status"] not in {"draft", "ready_for_release"} or draft["created_by"] != identity.subject:
            raise APIError(409, "edit_set_not_rebasable",
                           "Only the author may rebase an open or frozen edit set that has not been released, superseded or failed.")
        patches = repo.list_patches(parsed_edit_id)
        document = repo.get(parsed_id)
        if document is None or document["current_version_id"] is None:
            raise APIError(404, "not_found", "The requested document was not found.")
        new_version_id = UUID(str(document["current_version_id"]))
        if new_version_id == UUID(str(draft["base_version_id"])):
            raise APIError(409, "rebase_not_required", "The document has not changed since this draft was created.")
        if document["document_category"] != "insurer-authored":
            raise APIError(403, "document_category_not_authorized", "The current document classification does not permit editing.")
    if not patches:
        raise APIError(422, "patch_set_empty", "An edit set without patches has nothing to rebase.")
    version, source = _source_version(request, identity, parsed_id, new_version_id)
    try:
        report = request.app.state.restricted_parser.inspect(
            request.app.state.document_storage.root / str(version["storage_key"]),
            filename=str(draft["filename"]), include_targets=True,
            target_ids=[str(patch["target_id"]) for patch in patches])
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
        if target is None:
            conflicts.append({"target_id": str(patch["target_id"]), "reason": "target_missing_in_current_version"})
            continue
        if target.get("editable") is not True:
            conflicts.append({"target_id": str(patch["target_id"]), "reason": f"target_not_editable:{target.get('reason') or 'unsupported'}"})
            continue
        if target["value"].get("type") != expected.get("type") or target["value"].get("value") != expected.get("value"):
            conflicts.append({"target_id": str(patch["target_id"]), "reason": "base_value_changed"})
            continue
        # New patch rows for the successor; the predecessor's rows (and frozen plan) stay untouched.
        rebased.append({"target_id": str(patch["target_id"]), "expected_old_value": target["value"],
                        "expected_fingerprint": target["fingerprint"], "new_value": patch["new_value"]})
    if conflicts:
        raise APIError(409, "rebase_conflicts", "The current document version changed one or more targeted values.",
                       details=conflicts)
    new_edit_set_id = uuid4()
    try:
        with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
            outcome = DocumentRepository(connection, identity.tenant_id).supersede_with_rebased_draft(
                predecessor_id=parsed_edit_id, successor_id=new_edit_set_id, document_id=parsed_id,
                new_version_id=new_version_id, actor_id=identity.subject, patches=rebased)
    except ValueError as error:
        code = str(error)
        if code in {"edit_set_not_rebasable", "stale_base_version", "rebase_not_required"}:
            raise APIError(409, code, "The edit set changed while it was being rebased; reload and try again.") from None
        if code == "document_category_not_authorized":
            raise APIError(403, code, "The current document classification does not permit editing.") from None
        raise APIError(409, "rebase_conflict", "The draft could not be rebased against the current version.") from None
    except (SQLAlchemyError, RuntimeError, PermissionError):
        # Includes the unique successor index: a concurrent rebase already created the successor.
        raise APIError(409, "rebase_conflict", "The draft could not be rebased against the current version.") from None
    return {"previous_edit_set_id": str(parsed_edit_id), "edit_set_id": str(new_edit_set_id),
            "base_version_id": str(new_version_id), "status": "draft", "patches": len(rebased),
            "previous_status": outcome["previous_status"], "previous_frozen_hash": outcome["frozen_hash"],
            "previous_base_version_id": str(outcome["from_version_id"]), "predecessor_status": "superseded"}


@router.post("/{document_id}/edit-sets/{edit_set_id}/release")
def release_edit_set(document_id: str, edit_set_id: str, request: Request,
                     identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, object]:
    """Apply an owner-authorized frozen plan, verify and scan it before registration."""
    _require_owner(identity)
    try:
        parsed_document, parsed_edit_set = UUID(document_id), UUID(edit_set_id)
    except ValueError:
        raise APIError(404, "not_found", "The edit set was not found.") from None
    try:
        with claim_release(request.app.state.engine, str(parsed_edit_set)):
            return _release_edit_set_claimed(parsed_document, parsed_edit_set, request, identity)
    except ReleaseInProgress:
        raise APIError(409, "release_in_progress", "A release attempt for this edit set is already running.") from None


def _release_edit_set_claimed(parsed_document: UUID, parsed_edit_set: UUID, request: Request,
                              identity: AuthenticatedIdentity) -> dict[str, object]:
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        edit_set = repo.get_edit_set(parsed_edit_set)
        if edit_set is None or edit_set["document_id"] != parsed_document:
            raise APIError(404, "not_found", "The edit set was not found.")
        if edit_set["status"] == "released" and edit_set["released_version_id"] is not None:
            versions = repo.list_versions(parsed_document)
            released = next((v for v in versions if str(v["version_id"]) == str(edit_set["released_version_id"])), None)
            return {"edit_set_id": str(parsed_edit_set), "version_id": str(edit_set["released_version_id"]),
                    "outcome": (released or {}).get("verification_outcome", "INCOMPLETE"),
                    "download_available": is_download_eligible(
                        (released or {}).get("verification_outcome"), (released or {}).get("verification_adapter"), {
                            "verification_profile": (released or {}).get("verification_profile"),
                            "structural_checks": (released or {}).get("structural_checks"),
                            "visual_checks": (released or {}).get("visual_checks"),
                        }), "idempotent": True}
        if edit_set["status"] != "ready_for_release":
            raise APIError(409, "edit_set_not_ready_for_release", "Preflight and freeze this edit set before release.")
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
    docx_mode = request.app.state.settings.channel360_docx_verification_mode
    workflow = _release_workflow(adapter_name, docx_mode=docx_mode,
                                 pdf_verifier=RestrictedPdfVerifier(request.app.state.restricted_parser))
    try:
        result = workflow.release(source=source, edit_set=frozen, allow_incomplete=True)
    except PdfVerificationWorkerUnavailable as error:
        raise APIError(503, "pdf_verification_worker_unavailable",
                       "Isolated PDF verification could not complete; the frozen edit set is available for retry.",
                       details=[{"reason_code": error.reason_code}]) from None
    base_manifest: dict[str, object] = {
        "source_sha256": str(version["sha256"]), "adapter": adapter_name,
        "adapter_version": adapter_version, "edit_set_id": str(parsed_edit_set),
        "plan_sha256": frozen.plan.plan_sha256,
    }
    if result.verification is not None:
        base_manifest["verification"] = result.verification.as_dict()
    if adapter_name == "docx":
        base_manifest.update({"verification_profile": docx_mode,
                              "structural_checks": (result.verification.structural_checks if result.verification else "FAIL"),
                              "visual_checks": (result.verification.visual_checks if result.verification else "NOT_VERIFIED")})
    outcome = result.status
    staged = None
    stored = None
    if outcome in {"VERIFIED", "STRUCTURALLY_VERIFIED", "INCOMPLETE"} and result.derived_bytes is not None:
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
            reason_code = scan.reason_code or ("scanner_hash_mismatch" if scan.status == "clean" else "scanner_unavailable")
            raise APIError(503, "release_scan_unavailable",
                           "The derived file could not be confirmed clean; this frozen edit set is available for retry.",
                           details=[{"reason_code": reason_code}])
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
                "download_available": is_download_eligible("BLOCKED", adapter_name, base_manifest),
                "verification": result.verification.as_dict() if result.verification else None}
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
            "download_available": is_download_eligible(outcome, adapter_name, base_manifest),
            "verification": result.verification.as_dict() if result.verification else None}


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
            allowed = bool(item and is_download_eligible(
                item.get("verification_outcome"), item.get("verification_adapter"), {
                    "verification_profile": item.get("verification_profile"),
                    "structural_checks": item.get("structural_checks"),
                    "visual_checks": item.get("visual_checks"),
                }))
            if not allowed:
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
