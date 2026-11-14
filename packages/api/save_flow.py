"""One-call "Save as next version": freeze, preflight, verify and release through the existing engine.

The UI never sees drafts, patches, preflight, release or manifests; this module drives them internally and
reports one plain outcome.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from packages.api.auth import AuthenticatedIdentity
from packages.api.db_context import api_tenant_transaction
from packages.api.documents import (EditSetDraftRequest, PatchBatchRequest, PatchInput, _source_version,
                                    add_edit_set_patches, create_edit_set_draft, preflight_edit_set,
                                    release_edit_set)
from packages.api.errors import APIError
from packages.core import extraction
from packages.registry.documents import DocumentRepository
from packages.workers.parser import ParserNotVerified

FORMULA_NOTE = "Formulas update when opened in Excel."
TRACKING_NOTE = ("Track Changes is turned on in this document. Your edit is kept as a new version here, "
                 "not as a Word tracked change.")


def tracking_setting_on(source: bytes, filename: str) -> bool:
    """DOCX with the Track Changes *setting* enabled (distinct from unresolved tracked insertions/deletions)."""
    if not filename.lower().endswith(".docx") or not source.startswith(b"PK"):
        return False
    try:
        import zipfile
        with zipfile.ZipFile(io.BytesIO(source)) as zf:
            info = zf.getinfo("word/settings.xml")
            return info.file_size < 4_000_000 and b"trackRevisions" in zf.read(info)
    except (KeyError, zipfile.BadZipFile, OSError):
        return False

PLAIN = {
    "max_length_exceeded": "This value doesn't fit the field.",
    "widget_overflow": "This value doesn't fit the field.",
    "unsupported_glyph": "This text has characters the field can't display.",
    "unsupported_control_character": "This text has characters that can't be saved.",
    "required_field_empty": "This field can't be empty.",
    "invalid_choice": "Pick one of the listed options.",
    "invalid_checkbox_value": "That isn't a valid checkbox value.",
    "type_mismatch": "That value isn't the right kind of value for this cell.",
    "fingerprint_mismatch": "This document changed since it was opened. Reload it and try again.",
    "stale_source": "This document changed since it was opened. Reload it and try again.",
    "stale_base_version": "This document changed since it was opened. Reload it and try again.",
    "expected_value_mismatch": "This document changed since it was opened. Reload it and try again.",
    "target_not_editable_or_stale": "This value can't be changed here.",
    "target_not_found": "This value can't be found any more.",
    "unsafe_csv_value": "That value could be read as a formula, so it can't be saved.",
    "value_not_representable_in_encoding": "This text has characters the file's encoding can't hold.",
    "data_validation_value_invalid": "That value isn't allowed by the cell's validation rule.",
    "no_change": "Nothing changed, so there is no new version to save.",
    "no_change_has_no_candidate": "Nothing changed, so there is no new version to save.",
    "patch_set_empty": "Nothing changed, so there is no new version to save.",
    "release_scan_unavailable": "The file couldn't be scanned right now. Try again in a moment.",
    "pdf_verification_worker_unavailable": "Saving is unavailable right now. Try again in a moment.",
    "document_not_editable": "This file can't be edited.",
    "document_category_not_authorized": "This file can't be edited.",
    "docx_text_requires_string": "That value isn't valid text.",
    "invalid_integer": "That value isn't a whole number.",
    "invalid_number": "That value isn't a number.",
    "invalid_boolean": "That value isn't yes or no.",
    "invalid_date": "That value isn't a valid date (use YYYY-MM-DD).",
    "invalid_correction_value": "That value isn't valid for this cell.",
    "empty_document": "The edited document is empty. Add some content before saving.",
    "invalid_json": "This isn't valid JSON yet. Fix it and save again.",
    "invalid_xml": "This isn't well-formed XML yet. Fix it and save again.",
    "formatting_not_preserved": "This edit couldn't be saved without changing other text, so nothing was saved. Your edits are still here.",
    "text_too_large": "This text is too large to save.",
    "glyph_unavailable": "This text uses a font that doesn't include one of the new characters. Use characters already present in the line.",
    "overflow": "The new text is too long for its space and would run into neighbouring content. Shorten it.",
    "stale_target": "This text changed since it was selected. Reopen the version and try again.",
    "no_change": "There is no change to save.",
    "shared_operator": "This text is drawn together with neighbouring text and can't be changed on its own.",
    "font_unsupported": "This text uses a font encoding that can't be edited safely.",
    "font_metrics_unknown": "This text uses a font without size data, so a safe fit check isn't possible.",
    "undecodable_text": "This text can't be read reliably enough to edit.",
    "multiline_not_supported": "Edit one line at a time.",
    "not_visible": "This text is outside the visible page, so it can't be edited.",
    "marked_content_unsupported": "This text is wrapped in tagged content that can't be updated safely.",
    "unknown_target": "That text could not be found.",
    "pdf_verification_failed": "The edited PDF didn't pass the safety checks, so nothing was saved. Your edits are still here.",
}
GENERIC = "This change couldn't be saved safely. Your edits are still here."


def plain(code: str, locked: str | None = None) -> str:
    code = str(code or "")
    if code in PLAIN:
        return PLAIN[code]
    tail = code.split(":")[-1]
    if tail in PLAIN:
        return PLAIN[tail]
    from packages.api.workspace import cell_reason
    return cell_reason(code) if locked is None else locked


class SaveChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(min_length=1, max_length=500)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    value: str | bool | int | float


class PdfTextEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=3, max_length=300, pattern=r"^p\d+:[^\s]+:\d+-\d+$")
    old: str = Field(max_length=2000)
    new: str = Field(max_length=2000)


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: list[SaveChange] = Field(default_factory=list, max_length=500)
    text: str | None = Field(default=None, max_length=extraction.MAX_TEXT_BYTES)
    html: str | None = Field(default=None, max_length=extraction.MAX_TEXT_BYTES)  # formatted Markdown/HTML edits
    pdf_edits: list[PdfTextEdit] | None = Field(default=None, max_length=200)      # ordinary-PDF text edits


class SaveRejected(APIError):
    def __init__(self, code: str, message: str | None = None, status: int = 422):
        super().__init__(status, code, message or plain(code))


def _coerce(old_type: str, raw: Any) -> dict[str, Any]:
    if old_type in {"string", "choice"}:
        return {"type": old_type, "value": str(raw)}
    text = str(raw).strip()
    if old_type == "null":
        # An empty template field: numbers go in as numbers, everything else as text exactly as typed.
        if isinstance(raw, bool):
            return {"type": "boolean", "value": raw}
        if re.fullmatch(r"-?(?:0|[1-9]\d{0,14})", text):
            return {"type": "integer", "value": int(text)}
        if re.fullmatch(r"-?(?:0|[1-9]\d{0,8})\.\d{1,6}", text):
            return {"type": "decimal", "value": text}
        return {"type": "string", "value": str(raw)}
    if old_type == "integer":
        if isinstance(raw, bool) or not re.fullmatch(r"-?\d+", text):
            raise SaveRejected("invalid_integer")
        return {"type": "integer", "value": int(text)}
    if old_type == "decimal":
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise SaveRejected("invalid_number") from None
        if not number.is_finite():
            raise SaveRejected("invalid_number")
        return {"type": "decimal", "value": text}
    if old_type == "boolean":
        if isinstance(raw, bool):
            return {"type": "boolean", "value": raw}
        lowered = text.lower()
        if lowered in {"true", "yes", "1", "on"}:
            return {"type": "boolean", "value": True}
        if lowered in {"false", "no", "0", "off"}:
            return {"type": "boolean", "value": False}
        raise SaveRejected("invalid_boolean")
    if old_type in {"date", "datetime", "time"}:
        return {"type": old_type, "value": text}
    raise SaveRejected("type_mismatch")


def _guard_ready(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID) -> None:
    """Any version of a ready document may be edited; the save becomes the next (latest) version."""
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        repo = DocumentRepository(connection, identity.tenant_id)
        document = repo.get(document_id)
        version = repo.get_version(document_id, version_id) if document else None
    if document is None or version is None:
        raise APIError(404, "not_found", "The requested document was not found.")
    if document["ingestion_state"] != "ready":
        raise APIError(409, "document_not_ready", "The document is not ready for editing.")


def save_native(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID,
                changes: list[SaveChange]) -> dict[str, Any]:
    """Patch values through the format's verified adapter and release one new same-format version."""
    if not changes:
        raise SaveRejected("patch_set_empty", status=409)
    version, _source = _source_version(request, identity, document_id, version_id)
    tracking = tracking_setting_on(_source, str(version.get("filename", "")))
    ids = [change.target_id for change in changes]
    if len(set(ids)) != len(ids):
        raise SaveRejected("duplicate_target", GENERIC)
    try:
        report = request.app.state.restricted_parser.inspect(
            request.app.state.document_storage.root / str(version["storage_key"]),
            filename=str(version.get("filename", "")), include_targets=True, target_ids=ids)
    except ParserNotVerified:
        raise APIError(503, "view_not_verified", "Saving is unavailable right now. Try again in a moment.") from None
    targets = {item["id"]: item for item in report.targets}
    patches = []
    for change in changes:
        target = targets.get(change.target_id)
        if target is None or target.get("fingerprint") != change.fingerprint:
            raise SaveRejected("fingerprint_mismatch", status=409)
        if target.get("editable") is not True:
            raise SaveRejected(str(target.get("reason") or "not_editable"),
                               plain(str(target.get("reason") or ""), "This value is locked and can't be changed here."))
        patches.append(PatchInput(target_id=change.target_id, fingerprint=change.fingerprint,
                                  new_value=_coerce(str(target["value"]["type"]), change.value)))
    with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
        DocumentRepository(connection, identity.tenant_id).authorize_owner_upload(document_id, actor_id=identity.subject)
    # Edit from the opened version, whatever its position; the verified engine checks every precondition against it.
    request.state.edit_any_version = True
    request.state.edit_base_version_id = version_id
    try:
        draft = create_edit_set_draft(str(document_id), EditSetDraftRequest(reason="Saved from the workspace"), request, identity)
        edit_set_id = str(draft["edit_set_id"])
        add_edit_set_patches(str(document_id), edit_set_id, PatchBatchRequest(patches=patches), request, identity)
        preflight_edit_set(str(document_id), edit_set_id, request, identity)
        released = release_edit_set(str(document_id), edit_set_id, request, identity)
    except APIError as error:
        raise SaveRejected(error.code, plain(error.code), 409 if error.status_code == 409 else 422) from None
    outcome = str(released.get("outcome"))
    if outcome == "BLOCKED" or not released.get("version_id"):
        raise SaveRejected("blocked", _blocked_message(released))
    notes = [n for n in (FORMULA_NOTE if outcome == "INCOMPLETE" else None, TRACKING_NOTE if tracking else None) if n]
    return {"version_id": released["version_id"], "outcome": outcome, "note": " ".join(notes) or None}


def _blocked_message(released: dict[str, Any]) -> str:
    checks = (released.get("verification") or {}).get("checks") or []
    for check in checks:
        if check.get("status") == "FAIL":
            detail = check.get("detail") or {}
            for key in ("reason", "reason_code", "code"):
                if str(detail.get(key, "")) in PLAIN:
                    return PLAIN[str(detail[key])]
    return GENERIC


# ---------------------------------------------------------------------------------------------------------
# Same-format text writer (TXT, MD, HTML, XML and JSON edits that change structure)
# ---------------------------------------------------------------------------------------------------------

def _validate_text(suffix: str, text: str) -> None:
    if not text.strip():
        raise SaveRejected("empty_document")
    if suffix == ".json":
        try:
            json.loads(text)
        except (ValueError, RecursionError):
            raise SaveRejected("invalid_json") from None
    elif suffix == ".xml":
        from lxml import etree
        try:
            etree.fromstring(text.encode("utf-8"), etree.XMLParser(resolve_entities=False, no_network=True,
                                                                  load_dtd=False, huge_tree=False))
        except (etree.XMLSyntaxError, ValueError):
            raise SaveRejected("invalid_xml") from None


def encode_like(original: bytes, text: str) -> tuple[bytes, str]:
    """Encode edited text in the original's encoding and line endings, verifying that it decodes back."""
    _decoded, encoding = extraction.decode_text(original)
    crlf = b"\r\n" in original and not re.search(rb"(?<!\r)\n", original)
    body = text.replace("\r\n", "\n")
    if crlf:
        body = body.replace("\n", "\r\n")
    try:
        data = body.encode(encoding)
        back = data.decode(encoding)
    except (UnicodeEncodeError, UnicodeDecodeError):
        raise SaveRejected("value_not_representable_in_encoding") from None
    if back.lstrip("﻿") != body:
        raise SaveRejected("value_not_representable_in_encoding")
    return data, encoding


def save_text(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID,
              text: str, suffix: str) -> dict[str, Any]:
    version, source = _source_version(request, identity, document_id, version_id)
    _validate_text(suffix, text)
    data, encoding = encode_like(source, text)
    if data == source:
        raise SaveRejected("no_change", status=409)
    storage = request.app.state.document_storage
    scanner = request.app.state.ingestion_pipeline.scanner
    staged = storage.stage(identity.tenant_id, document_id, uuid4(), io.BytesIO(data),
                           max_bytes=request.app.state.settings.max_upload_bytes)
    try:
        scan = scanner.scan(staged.path)
    except Exception:  # noqa: BLE001 - scanner failures fail closed
        scan = None
    if scan is None or scan.status != "clean" or scan.sha256 != staged.sha256:
        infected = scan is not None and scan.status == "infected"
        if infected:
            storage.quarantine(staged)
            raise SaveRejected("blocked", "The saved file didn't pass the safety scan.")
        storage.discard_staged(staged)
        raise SaveRejected("release_scan_unavailable", status=503)
    stored = storage.promote_derived(staged, scan, edit_set_id=uuid4())
    try:
        with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
            new_version = DocumentRepository(connection, identity.tenant_id).commit_text_version(
                document_id=document_id, base_version_id=version_id, stored=stored,
                media_type=str(version.get("mime") or "text/plain"), actor_id=identity.subject,
                details={"writer": "text", "encoding": encoding, "suffix": suffix,
                         "decode_verified": True, "malware_scan": scan.status, "source_sha256": str(version["sha256"])})
    except ValueError:
        raise SaveRejected("stale_base_version", status=409) from None
    except (SQLAlchemyError, RuntimeError, PermissionError):
        raise APIError(503, "save_unavailable", "Saving is unavailable right now. Try again in a moment.") from None
    # Honest outcome: the text was decoded in the original encoding, parsed where it has a grammar and scanned,
    # but no independent native verifier exists for these formats, so it is SAVED rather than VERIFIED.
    return {"version_id": str(new_version), "outcome": "SAVED", "note": None}


def _pointer_leaves(value: Any, pointer: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        leaves: dict[str, Any] = {}
        for key, child in value.items():
            leaves.update(_pointer_leaves(child, f"{pointer}/{str(key).replace('~', '~0').replace('/', '~1')}"))
        return leaves
    if isinstance(value, list):
        leaves = {}
        for index, child in enumerate(value):
            leaves.update(_pointer_leaves(child, f"{pointer}/{index}"))
        return leaves
    return {pointer: value}


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _shape(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_shape(child) for child in value]
    return None


def json_value_changes(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID,
                       text: str) -> list[SaveChange] | None:
    """Scalar value edits of unchanged structure map onto the JSON adapter; anything else returns ``None``."""
    version, source = _source_version(request, identity, document_id, version_id)
    try:
        old, new = json.loads(source.decode("utf-8-sig")), json.loads(text)
    except (ValueError, RecursionError, UnicodeDecodeError):
        return None
    if _shape(old) != _shape(new):
        return None
    old_leaves, new_leaves = _pointer_leaves(old), _pointer_leaves(new)
    changed = [p for p, v in new_leaves.items() if v != old_leaves[p] or type(v) is not type(old_leaves[p])]
    if not changed or len(changed) > 200:
        return None
    ids = [f"json:{pointer}" for pointer in changed]
    try:
        report = request.app.state.restricted_parser.inspect(
            request.app.state.document_storage.root / str(version["storage_key"]),
            filename=str(version.get("filename", "")), include_targets=True, target_ids=ids)
    except ParserNotVerified:
        return None
    targets = {item["id"]: item for item in report.targets}
    result = []
    for pointer in changed:
        target = targets.get(f"json:{pointer}")
        value = new_leaves[pointer]
        if target is None or not target.get("editable") or value is None or type(value) is not type(old_leaves[pointer]):
            return None
        result.append(SaveChange(target_id=target["id"], fingerprint=target["fingerprint"], value=value))
    return result


def suffix_of(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def save_rich(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID,
              edited_html: str, suffix: str) -> dict[str, Any]:
    """Serialise formatted edits back to Markdown/HTML, proving untouched blocks render the same text."""
    from packages.core import richtext
    _version, source = _source_version(request, identity, document_id, version_id)
    original, _encoding = extraction.decode_text(source)
    fmt = "md" if suffix in {".md", ".markdown"} else "html"
    try:
        new_text = richtext.serialise(fmt, original, edited_html)
        richtext.check_untouched_unchanged(fmt, original, new_text, edited_html)
    except richtext.RichTextRejected as error:
        raise SaveRejected(error.code, status=422) from None
    return save_text(request, identity, document_id, version_id, new_text, suffix)


def _commit_derived_bytes(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID,
                          version: dict[str, Any], data: bytes, details: dict[str, Any]) -> UUID:
    """Scan, promote and attach one same-format derived version (shared by the byte-level writers)."""
    storage = request.app.state.document_storage
    scanner = request.app.state.ingestion_pipeline.scanner
    staged = storage.stage(identity.tenant_id, document_id, uuid4(), io.BytesIO(data),
                           max_bytes=request.app.state.settings.max_upload_bytes)
    try:
        scan = scanner.scan(staged.path)
    except Exception:  # noqa: BLE001 - scanner failures fail closed
        scan = None
    if scan is None or scan.status != "clean" or scan.sha256 != staged.sha256:
        if scan is not None and scan.status == "infected":
            storage.quarantine(staged)
            raise SaveRejected("blocked", "The saved file didn't pass the safety scan.")
        storage.discard_staged(staged)
        raise SaveRejected("release_scan_unavailable", status=503)
    stored = storage.promote_derived(staged, scan, edit_set_id=uuid4())
    try:
        with request.app.state.engine.connect() as connection, api_tenant_transaction(connection, identity):
            return DocumentRepository(connection, identity.tenant_id).commit_text_version(
                document_id=document_id, base_version_id=version_id, stored=stored,
                media_type=str(version.get("mime") or "application/octet-stream"), actor_id=identity.subject,
                details={**details, "malware_scan": scan.status, "source_sha256": str(version["sha256"])})
    except ValueError:
        raise SaveRejected("stale_base_version", status=409) from None
    except (SQLAlchemyError, RuntimeError, PermissionError):
        raise APIError(503, "save_unavailable", "Saving is unavailable right now. Try again in a moment.") from None


def pdf_edit_candidate(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID,
                       edits: list[PdfTextEdit]) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """Apply text edits in the sandboxed worker and verify the result. Raises SaveRejected with plain wording."""
    from packages.core import pdf_signature
    from packages.workers.pdf_edit import PdfEditRefused, PdfEditUnavailable
    version, source = _source_version(request, identity, document_id, version_id)
    sig = pdf_signature.classify(source)
    if pdf_signature.blocks_editing(sig["status"]):
        raise SaveRejected("pdf_signed", pdf_signature.REASONS[sig["status"]], status=409)
    path = request.app.state.document_storage.root / str(version["storage_key"])
    try:
        data, report = request.app.state.pdf_editor.edit(path, [e.model_dump() for e in edits])
    except PdfEditRefused as refused:
        raise SaveRejected(refused.code, plain(refused.code)) from None
    except PdfEditUnavailable:
        raise APIError(503, "pdf_edit_unavailable", "PDF editing is unavailable right now. Try again in a moment.") from None
    if report.get("outcome") != "VERIFIED" or hashlib.sha256(data).hexdigest() != report.get("output_sha256"):
        raise SaveRejected("pdf_verification_failed", status=422)
    return data, report, version


def save_pdf_text(request: Request, identity: AuthenticatedIdentity, document_id: UUID, version_id: UUID,
                  edits: list[PdfTextEdit]) -> dict[str, Any]:
    data, report, version = pdf_edit_candidate(request, identity, document_id, version_id, edits)
    new_version = _commit_derived_bytes(request, identity, document_id, version_id, version, data, {
        "writer": "pdf_text", "outcome": "VERIFIED", "edit_count": len(edits),
        "edited_pages": sorted({e["page"] + 1 for e in report["edits"]}),
        "edit_targets": [e.id for e in edits], "checks": report["checks"], "decode_verified": True,
        "font_substitutions": [{"id": e["id"], "substitute": e["substitute_font"]} for e in report["edits"] if e.get("substitute_font")]})
    return {"version_id": str(new_version), "outcome": "VERIFIED", "note": substitution_note(report)}


def substitution_note(report: dict[str, Any]) -> str | None:
    n = sum(1 for e in report["edits"] if e.get("substitute_font"))
    if not n:
        return None
    return (f"{n} edited line{'s' if n != 1 else ''} use a similar standard font because the original font doesn't include every new character.")
