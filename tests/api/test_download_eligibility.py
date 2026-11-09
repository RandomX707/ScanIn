from __future__ import annotations

import hashlib
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from packages.api.app import create_app
from packages.api.auth import create_development_token
from packages.api.settings import Settings
from packages.storage.local import ImmutableLocalStorage
import packages.api.documents as documents_api


def _settings(tmp_path, mode: str) -> Settings:
    return Settings(
        environment="test", auth_mode="development",
        development_auth_secret=SecretStr("api-download-test-secret-at-least-32-bytes"),
        database_url=SecretStr("postgresql+psycopg://test:unused@127.0.0.1:55439/test"),
        queue_url="redis://127.0.0.1:6379/0", storage_root=str(tmp_path / "storage"),
        channel360_docx_verification_mode=mode,
    )


@pytest.mark.parametrize(("outcome", "adapter", "manifest", "expected_status"), [
    ("VERIFIED", "csv", {}, 200),
    ("INCOMPLETE", "xlsx", {}, 409),
    ("VERIFIED", "pdf", {}, 200),
    ("STRUCTURALLY_VERIFIED", "docx", {
        "verification_profile": "structural", "structural_checks": "PASS",
        "visual_checks": "NOT_PERFORMED",
    }, 200),
])
def test_versions_eligibility_matches_download_api_even_after_docx_mode_changes(
        monkeypatch, tmp_path, outcome, adapter, manifest, expected_status):
    """A historical structural DOCX release remains downloadable under today's strict mode."""
    tenant_id, document_id, original_id, derived_id = uuid4(), uuid4(), uuid4(), uuid4()
    original_bytes, derived_bytes = b"synthetic original", b"synthetic derived"
    items = [{
        "version_id": str(original_id), "kind": "original", "sha256": hashlib.sha256(original_bytes).hexdigest(),
        "download_available": True,
    }, {
        "version_id": str(derived_id), "kind": "derived", "sha256": hashlib.sha256(derived_bytes).hexdigest(),
        "verification_outcome": outcome, "verification_adapter": adapter,
        "verification_profile": manifest.get("verification_profile"),
        "structural_checks": manifest.get("structural_checks"),
        "visual_checks": manifest.get("visual_checks"),
        "download_available": outcome == "VERIFIED" or (outcome == "STRUCTURALLY_VERIFIED" and
            adapter == "docx" and manifest.get("verification_profile") == "structural" and
            manifest.get("structural_checks") == "PASS" and manifest.get("visual_checks") == "NOT_PERFORMED"),
    }]
    storage_root = tmp_path / "storage"
    storage = ImmutableLocalStorage(storage_root)
    versions = {}
    for version_id, kind, content in ((original_id, "original", original_bytes), (derived_id, "derived", derived_bytes)):
        digest = hashlib.sha256(content).hexdigest()
        key = f"{tenant_id}/{kind}/{document_id}/{digest}"
        path = storage.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        versions[str(version_id)] = {
            "version_id": version_id, "kind": kind, "filename": f"synthetic.{adapter}",
            "storage_key": key, "size_bytes": len(content), "sha256": digest,
            "mime": "application/octet-stream",
        }

    class _Engine:
        def connect(self):
            return nullcontext(object())

        def dispose(self):
            pass

    class _Repository:
        def __init__(self, connection, tenant):
            assert tenant == tenant_id

        def get(self, requested_document):
            return {"id": document_id} if requested_document == document_id else None

        def list_versions(self, requested_document):
            assert requested_document == document_id
            return items

        def get_version(self, requested_document, requested_version):
            assert requested_document == document_id
            return versions.get(str(requested_version))

    monkeypatch.setattr(documents_api, "api_tenant_transaction", lambda connection, identity: nullcontext(connection))
    monkeypatch.setattr(documents_api, "DocumentRepository", _Repository)
    app = create_app(_settings(tmp_path, "structural_and_visual"))
    app.state.engine.dispose()
    app.state.engine = _Engine()
    app.state.document_storage = storage
    token = create_development_token(app.state.settings, subject="synthetic-owner", tenant_id=tenant_id, role="OWNER")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(app) as client:
            listed = client.get(f"/api/documents/{document_id}/versions", headers=headers)
            assert listed.status_code == 200
            returned = listed.json()["items"]
            for item in returned:
                response = client.get(
                    f"/api/documents/{document_id}/versions/{item['version_id']}/download", headers=headers)
                if item["kind"] == "original":
                    assert response.status_code == 200
                    assert response.content == original_bytes
                else:
                    assert bool(item["download_available"]) is (response.status_code == 200)
                    assert response.status_code == expected_status
                    if response.status_code == 200:
                        assert response.content == derived_bytes
                        assert hashlib.sha256(response.content).hexdigest() == item["sha256"]
    finally:
        app.state.engine = None


def test_strict_profile_preflight_rejects_structural_only_docx_result(monkeypatch, tmp_path):
    tenant_id, document_id, edit_set_id, version_id = uuid4(), uuid4(), uuid4(), uuid4()
    actor = "synthetic-owner"
    source = b"synthetic docx source"
    source_sha256 = hashlib.sha256(source).hexdigest()
    patch = {
        "target_id": "docx:body:p0:r0:t0", "expected_fingerprint": "a" * 64,
        "expected_old_value": {"type": "string", "value": "old"},
        "new_value": {"type": "string", "value": "new"},
    }
    edit_set = {
        "document_id": document_id, "status": "draft", "created_by": actor,
        "authorized_category": "insurer-authored", "document_category": "insurer-authored",
        "base_version_id": version_id, "current_version_id": version_id,
        "filename": "synthetic.docx", "reason": "synthetic correction",
    }

    class _Engine:
        def connect(self):
            return nullcontext(object())

        def dispose(self):
            pass

    class _Repository:
        def __init__(self, connection, tenant):
            assert tenant == tenant_id

        def get_edit_set(self, requested_edit_set):
            return edit_set if requested_edit_set == edit_set_id else None

        def list_patches(self, requested_edit_set):
            assert requested_edit_set == edit_set_id
            return [patch]

    class _Workflow:
        def freeze(self, **kwargs):
            assert kwargs["role"] == "OWNER"
            return object()

        def release(self, **kwargs):
            return SimpleNamespace(status="STRUCTURALLY_VERIFIED")

    report = SimpleNamespace(sha256=source_sha256, adapter="docx", adapter_version="test",
                             targets=[{"id": patch["target_id"], "editable": True,
                                       "fingerprint": patch["expected_fingerprint"]}])
    settings = _settings(tmp_path, "structural_and_visual")
    app = create_app(settings)
    app.state.engine.dispose()
    app.state.engine = _Engine()
    app.state.restricted_parser = SimpleNamespace(inspect=lambda *args, **kwargs: report)
    monkeypatch.setattr(documents_api, "api_tenant_transaction", lambda connection, identity: nullcontext(connection))
    monkeypatch.setattr(documents_api, "DocumentRepository", _Repository)
    monkeypatch.setattr(documents_api, "_source_version",
                        lambda *args: ({"sha256": source_sha256, "storage_key": "synthetic/source.docx"}, source))

    def strict_workflow(adapter, *, docx_mode):
        assert adapter == "docx"
        assert docx_mode == "structural_and_visual"
        return _Workflow()

    monkeypatch.setattr(documents_api, "_release_workflow", strict_workflow)
    token = create_development_token(settings, subject=actor, tenant_id=tenant_id, role="OWNER")
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/api/documents/{document_id}/edit-sets/{edit_set_id}/preflight",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 422
        assert response.json()["code"] == "critical_docx_visual_verification_unavailable"
    finally:
        app.state.engine = None
