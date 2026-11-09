"""Phase 3A: sidecar data-correction fields from read-only targets and validated text anchors."""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from packages.api.auth import create_development_token
from tests.api.test_application import _settings, _test_app

PAGE_TEXT = "Invoice INV-2026-0042\r\nInsured: Rahul Sharma\r\nTotal due: 45,000.00"


def _app(monkeypatch, tmp_path, *, adapter="pdf", status="inspected", details=None, targets=(), current=True):
    import packages.api.documents as documents
    settings = _settings(storage_root=str(tmp_path / "storage"))
    app = _test_app(settings)
    tenant_id, document_id, version_id = uuid4(), uuid4(), uuid4()
    key = f"original/{tenant_id}/{document_id}/source"
    path = app.state.document_storage.root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    content = b"synthetic immutable source"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    row = {"ingestion_state": "ready", "filename": "source.pdf", "storage_key": key, "size_bytes": len(content),
           "sha256": digest, "current_version_id": version_id, "tenant_id": tenant_id}
    created: list[dict] = []

    class Engine:
        def connect(self): return nullcontext(object())
        def dispose(self): pass

    class Repo:
        def __init__(self, connection, tenant):
            assert tenant == tenant_id
        def get(self, doc_id):
            return row if doc_id == document_id else None
        def create_manual_field(self, **kwargs):
            created.append(kwargs)
            return uuid4()

    calls: list[dict] = []

    def inspect(*args, **kwargs):
        calls.append(kwargs)
        ids = kwargs.get("target_ids") or []
        return SimpleNamespace(status=status, adapter=adapter, adapter_version="0.1.0", sha256=digest,
                               targets=tuple(t for t in targets if t["id"] in ids), details=details,
                               targets_truncated=False, targets_page=None)

    monkeypatch.setattr(documents, "DocumentRepository", Repo)
    monkeypatch.setattr(documents, "api_tenant_transaction", lambda connection, identity: nullcontext(connection))
    app.state.engine = Engine()
    app.state.restricted_parser = SimpleNamespace(inspect=inspect)
    token = create_development_token(settings, subject="owner", tenant_id=tenant_id, role="OWNER")
    return app, {"Authorization": f"Bearer {token}"}, document_id, version_id, digest, created, calls


PDF_DETAILS = {"pages": [{"page": 1, "text": PAGE_TEXT, "scanned_or_image_only": False},
                         {"page": 2, "text": "", "scanned_or_image_only": True}], "fields": []}


def _anchor(version_id, **overrides):
    start = PAGE_TEXT.index("Rahul Sharma")
    body = {"source_version_id": str(version_id), "kind": "pdf_page", "page": 1, "start": start,
            "end": start + len("Rahul Sharma"), "text": "Rahul Sharma"}
    body.update(overrides)
    return {"text_anchor": body}


def test_pdf_text_selection_is_validated_and_observed_value_comes_from_the_source(monkeypatch, tmp_path):
    app, headers, document_id, version_id, digest, created, calls = _app(monkeypatch, tmp_path, details=PDF_DETAILS)
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{document_id}/fields", headers=headers,
                               json={**_anchor(version_id), "label": "Insured name"})
    assert response.status_code == 200, response.text
    body = response.json()
    start = PAGE_TEXT.index("Rahul Sharma")
    assert body["target_id"] == f"text:pdf:page:1:{start}-{start + 12}"
    assert body["observed_value"] == {"type": "string", "value": "Rahul Sharma", "native": "Rahul Sharma", "display": None}
    evidence = body["evidence"][0]
    assert evidence["kind"] == "text_selection" and evidence["source_sha256"] == digest
    assert evidence["source_version_id"] == str(version_id) and evidence["location"] == {"page": 1}
    assert evidence["context_before"].endswith("Insured: ") and evidence["context_after"].startswith("\r\nTotal due")
    assert evidence["text_sha256"] == hashlib.sha256(b"Rahul Sharma").hexdigest()
    assert body["target_fingerprint"] == created[0]["fingerprint"] and len(body["target_fingerprint"]) == 64
    assert created[0]["observed_value"]["value"] == "Rahul Sharma" and created[0]["label"] == "Insured name"
    assert calls[0]["include_targets"] is False


@pytest.mark.parametrize("overrides,status,code", [
    ({"text": "Rahul Sharme"}, 409, "text_selection_mismatch"),
    ({"start": 0, "end": 5, "text": "Rahul"}, 409, "text_selection_mismatch"),
    ({"page": 2, "start": 0, "end": 1, "text": "x"}, 422, "text_not_extractable"),
    ({"page": 9}, 422, "text_anchor_out_of_range"),
    ({"start": len(PAGE_TEXT) - 2, "end": len(PAGE_TEXT) + 3, "text": "00xyz"}, 422, "text_anchor_out_of_range"),
    ({"start": 21, "end": 23, "text": "\r\n"}, 422, "text_selection_empty"),
    ({"kind": "docx_block", "page": None, "block_id": "word/document.xml:p0"}, 422, "text_anchor_not_supported"),
    ({"start": 0, "end": 501, "text": "x" * 500}, 422, "text_anchor_out_of_range"),
])
def test_invalid_text_selections_fail_closed_with_specific_reasons(monkeypatch, tmp_path, overrides, status, code):
    app, headers, document_id, version_id, *_ , created, _calls = _app(monkeypatch, tmp_path, details=PDF_DETAILS)
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{document_id}/fields", headers=headers, json=_anchor(version_id, **overrides))
    assert (response.status_code, response.json().get("code")) == (status, code), response.text
    assert created == []


def test_stale_version_anchor_is_rejected_before_parsing(monkeypatch, tmp_path):
    app, headers, document_id, version_id, _d, created, calls = _app(monkeypatch, tmp_path, details=PDF_DETAILS)
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{document_id}/fields", headers=headers, json=_anchor(uuid4()))
    assert (response.status_code, response.json()["code"]) == (409, "stale_source_version")
    assert calls == [] and created == []


def test_docx_block_anchor_records_block_location(monkeypatch, tmp_path):
    details = {"blocks": [{"id": "word/document.xml:p3", "part": "word/document.xml", "paragraph_index": 3,
                           "kind": "paragraph", "text": "Policyholder: Asha Rao", "target_ids": []}]}
    app, headers, document_id, version_id, _d, created, _c = _app(monkeypatch, tmp_path, adapter="docx", details=details)
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{document_id}/fields", headers=headers, json={"text_anchor": {
            "source_version_id": str(version_id), "kind": "docx_block", "block_id": "word/document.xml:p3",
            "start": 14, "end": 22, "text": "Asha Rao"}})
    assert response.status_code == 200, response.text
    assert response.json()["target_id"] == "text:docx:word/document.xml:p3:14-22"
    assert response.json()["evidence"][0]["location"]["paragraph_index"] == 3
    assert response.json()["evidence"][0]["extraction"] == "docx_w_t_runs_excluding_deleted_text"


def test_read_only_native_target_can_back_a_sidecar_field(monkeypatch, tmp_path):
    target = {"id": "xlsx:Claims!B2", "label": "Insured", "fingerprint": "a" * 64, "editable": False,
              "reason": "macros_present", "value": {"type": "string", "value": "Asha", "native": "Asha", "display": None}}
    app, headers, document_id, version_id, digest, created, calls = _app(monkeypatch, tmp_path, adapter="xlsx", targets=(target,))
    with TestClient(app) as client:
        ok = client.post(f"/api/documents/{document_id}/fields", headers=headers,
                         json={"target_id": target["id"], "fingerprint": target["fingerprint"]})
        stale = client.post(f"/api/documents/{document_id}/fields", headers=headers,
                            json={"target_id": target["id"], "fingerprint": "b" * 64})
        missing = client.post(f"/api/documents/{document_id}/fields", headers=headers,
                              json={"target_id": "xlsx:Claims!Z9", "fingerprint": "a" * 64})
    assert ok.status_code == 200, ok.text
    assert ok.json()["evidence"][0] | {} == {**ok.json()["evidence"][0], "kind": "native_target", "natively_editable": False,
                                              "native_reason": "macros_present", "source_sha256": digest}
    assert calls[0]["target_ids"] == ["xlsx:Claims!B2"] and calls[0]["include_targets"] is True
    assert (stale.status_code, stale.json()["code"]) == (409, "target_not_found_or_stale")
    assert (missing.status_code, missing.json()["code"]) == (409, "target_not_found_or_stale")
    assert len(created) == 1


@pytest.mark.parametrize("status,adapter", [("identified", "office"), ("rejected", "pdf"), ("inspected", "text")])
def test_documents_without_a_safe_viewer_cannot_anchor_fields(monkeypatch, tmp_path, status, adapter):
    app, headers, document_id, version_id, *_rest = _app(monkeypatch, tmp_path, adapter=adapter, status=status, details=PDF_DETAILS)
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{document_id}/fields", headers=headers, json=_anchor(version_id))
    assert (response.status_code, response.json()["code"]) == (409, "document_not_viewable")


@pytest.mark.parametrize("body", [
    {},
    {"target_id": "csv:2:2"},
    {"fingerprint": "a" * 64},
    {"target_id": "csv:2:2", "fingerprint": "a" * 64, "text_anchor": {"source_version_id": str(uuid4()), "kind": "pdf_page",
                                                                     "page": 1, "start": 0, "end": 1, "text": "I"}},
])
def test_field_requests_need_exactly_one_source(monkeypatch, tmp_path, body):
    app, headers, document_id, *_rest = _app(monkeypatch, tmp_path, details=PDF_DETAILS)
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{document_id}/fields", headers=headers, json=body)
    assert (response.status_code, response.json()["code"]) == (422, "field_source_invalid")


def test_other_tenants_and_anonymous_callers_cannot_anchor_fields(monkeypatch, tmp_path):
    from packages.api.auth import create_development_token as token
    app, headers, document_id, version_id, *_rest, created, _c = _app(monkeypatch, tmp_path, details=PDF_DETAILS)
    import packages.api.documents as documents

    class OtherTenantRepo:
        def __init__(self, connection, tenant):
            self.tenant = tenant
        def get(self, doc_id):
            return None
    other = {"Authorization": "Bearer " + token(_settings(storage_root=str(tmp_path / "storage")), subject="x",
                                                 tenant_id=uuid4(), role="OWNER")}
    with TestClient(app) as client:
        anonymous = client.post(f"/api/documents/{document_id}/fields", json=_anchor(version_id))
        monkeypatch.setattr(documents, "DocumentRepository", OtherTenantRepo)
        foreign = client.post(f"/api/documents/{document_id}/fields", headers=other, json=_anchor(version_id))
    assert anonymous.status_code == 401 and foreign.status_code == 404 and created == []
