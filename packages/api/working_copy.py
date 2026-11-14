"""Digitally signed PDFs: truthful signature status and the explicit "Create editable working copy" action.

The signed original is never modified. A working copy is a NEW, unsigned, visibly marked derivative parented to the chosen
version; every later edit makes another version of that working copy. Nothing here signs, re-signs or simulates a signature.
"""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from packages.api.auth import AuthenticatedIdentity, require_owner
from packages.api.compare import lineage_classes, load_family
from packages.api.documents import _source_version
from packages.api.errors import APIError
from packages.api.save_flow import SaveRejected, _commit_derived_bytes
from packages.core import pdf_signature
from packages.workers.pdf_edit import PdfEditRefused, PdfEditUnavailable

router = APIRouter(prefix="/api", tags=["working-copy"])

ACKNOWLEDGEMENT = ("The working copy will NOT carry the original signer's digital signature or approval. It is a separate, unsigned, "
                   "modified document, marked \"DRAFT — MODIFIED COPY — NOT OFFICIAL\" on every page. The signed original stays exactly as received.")


def validate_signatures(request: Request, source: bytes, sha: str) -> dict[str, Any]:
    cache: dict = request.app.state.__dict__.setdefault("signature_validations", {})
    if sha not in cache:
        if len(cache) > 64:
            cache.clear()
        cache[sha] = request.app.state.signature_validator.validate(source)
    return cache[sha]


def signature_payload(validation: dict[str, Any]) -> dict[str, Any]:
    """Truthful, UI-facing signature state. `validated` is True only for integrity + a configured trust policy."""
    return {"status": validation["structure"], "validated": bool(validation["validated"]), "state": validation["state"], "label": validation["label"],
            "detail": validation["detail"], "trust_policy_configured": validation["trust_policy_configured"], "docmdp": validation["docmdp"],
            "signatures": [{k: v for k, v in s.items() if k in {"integrity", "trust", "trust_reason", "covers_whole_file", "reason", "subfilter", "timestamp_token_present", "signing_time_attribute_present", "signer", "state"}} for s in validation["signatures"]],
            "limits": validation["limits"]}


class WorkingCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledge: bool = False


@router.post("/documents/{document_id}/versions/{version_id}/working-copy")
def create_working_copy(document_id: str, version_id: str, payload: WorkingCopyRequest, request: Request,
                        identity: AuthenticatedIdentity = Depends(require_owner)) -> dict[str, Any]:
    try:
        doc, ver = UUID(document_id), UUID(version_id)
    except ValueError:
        raise APIError(404, "not_found", "The requested version was not found.") from None
    version, source = _source_version(request, identity, doc, ver)
    versions, events, meta = load_family(request, identity, doc)
    name = meta["filename"]
    if PurePosixPath(name).suffix.lower() != ".pdf" or not source.startswith(b"%PDF-"):
        raise APIError(415, "working_copy_pdf_only", "Working copies are available for PDF documents.")
    if lineage_classes(versions, events).get(str(ver)) == "unsigned_working_copy":
        raise APIError(409, "already_working_copy", "This version is already an unsigned working copy; edit it directly.")
    structure = pdf_signature.classify(source)
    if structure["status"] == "none":
        raise APIError(409, "not_signed", "This PDF isn't digitally signed, so it can be edited directly.")
    if not payload.acknowledge:
        raise APIError(422, "acknowledgement_required", "Confirm that the working copy will not keep the original signature.", details=[{"acknowledgement": ACKNOWLEDGEMENT}])
    sha = hashlib.sha256(source).hexdigest()
    validation = validate_signatures(request, source, sha)
    path = request.app.state.document_storage.root / str(version["storage_key"])
    try:
        data, report = request.app.state.pdf_editor.working_copy(path, name=name, state=validation["state"])
    except PdfEditRefused as refused:
        raise APIError(422, refused.code, "The working copy could not be created safely.") from None
    except PdfEditUnavailable:
        raise APIError(503, "working_copy_unavailable", "Creating a working copy is unavailable right now. Try again in a moment.") from None
    if report.get("outcome") != "VERIFIED" or hashlib.sha256(data).hexdigest() != report.get("output_sha256"):
        raise APIError(422, "working_copy_verification_failed", "The working copy could not be verified.")
    check = pdf_signature.classify(data)
    if check["status"] != "none":                       # independent re-check in the API process
        raise APIError(422, "working_copy_verification_failed", "The working copy could not be verified.")
    try:
        new_version = _commit_derived_bytes(request, identity, doc, ver, version, data, {
            "writer": "pdf_working_copy", "unsigned_working_copy": True, "outcome": "VERIFIED", "acknowledged_by": identity.subject,
            "source_signature_structure": structure["status"], "source_signature_state": validation["state"],
            "source_signature_count": len(validation["signatures"]), "checks": report["checks"],
            "signature_artefacts_removed": report["signature_artefacts_removed"]})
    except SaveRejected as rejected:
        raise APIError(rejected.status_code, rejected.code, rejected.message) from None
    request.app.state.__dict__.get("chunk_text_cache", {}).clear()
    return {"version_id": str(new_version), "outcome": "VERIFIED", "classification": "unsigned_working_copy",
            "note": "Unsigned working copy created. It is not the signed original and is marked as a modified copy."}
