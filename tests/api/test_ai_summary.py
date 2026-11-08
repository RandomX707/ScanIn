from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from packages.api.auth import create_development_token
from packages.api.documents import router
from packages.api.settings import Settings
from tests.api.test_application import _settings, _test_app


def test_ai_summary_requires_signed_identity(tmp_path):
    with TestClient(_test_app(_settings(storage_root=str(tmp_path / "storage")))) as client:
        response = client.post(f"/api/documents/{uuid4()}/versions/{uuid4()}/ai-summary")
    assert response.status_code == 401


def test_ai_summary_resolves_tenant_source_and_returns_provider_result(monkeypatch, tmp_path):
    import packages.api.documents as documents
    settings = _settings(storage_root=str(tmp_path / "storage"))
    app = _test_app(settings)
    tenant_id, document_id, version_id = uuid4(), uuid4(), uuid4()
    storage = app.state.document_storage
    key = f"original/{tenant_id}/{document_id}/source.csv"
    path = storage.root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    content = b"claim_number,policyholder\nSYN-1,Example Person\n"
    path.write_bytes(content)
    row = {"ingestion_state": "ready", "kind": "original", "filename": "claim.csv", "storage_key": key,
           "size_bytes": len(content), "sha256": __import__("hashlib").sha256(content).hexdigest()}
    class Engine:
        def connect(self): return nullcontext(object())
        def dispose(self): pass
    class Repo:
        def __init__(self, connection, tenant):
            assert tenant == tenant_id
        def get_version(self, doc_id, ver_id):
            return row if (doc_id, ver_id) == (document_id, version_id) else None
    monkeypatch.setattr(documents, "DocumentRepository", Repo)
    monkeypatch.setattr(documents, "api_tenant_transaction", lambda connection, identity: nullcontext(connection))
    app.state.engine = Engine()
    app.state.restricted_parser = SimpleNamespace(inspect=lambda *args, **kwargs: SimpleNamespace(
        status="inspected", adapter="csv", sha256=row["sha256"], structural_summary={"format": "csv"},
        summary_evidence=({"id": "csv:header", "location": "CSV header", "text": "claim_number | policyholder"},),
        targets_truncated=False))
    class Provider:
        calls = 0
        def explain(self, **kwargs):
            self.calls += 1
            assert kwargs["source_sha256"] == row["sha256"]
            assert kwargs["evidence"][0]["id"] == "csv:header"
            return {"source_document_id": str(document_id), "source_version_id": str(version_id),
                    "source_sha256": row["sha256"], "generation_method": "ai", "provider": "mock",
                    "model": "synthetic", "explanation": "Synthetic claim record.", "key_points": [],
                    "evidence_references": [], "limitations": []}
    app.state.llm_provider = Provider()
    token = create_development_token(settings, subject="processor", tenant_id=tenant_id, role="processor")
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{document_id}/versions/{version_id}/ai-summary",
                               headers={"Authorization": f"Bearer {token}"})
        additional = [client.post(f"/api/documents/{document_id}/versions/{version_id}/ai-summary",
                                  headers={"Authorization": f"Bearer {token}"}) for _ in range(5)]
    assert response.status_code == 200
    assert response.json()["source_sha256"] == row["sha256"]
    assert response.json()["explanation"] == "Synthetic claim record."
    app.state.restricted_parser.inspect = lambda *args, **kwargs: SimpleNamespace(
        status="inspected", adapter="csv", sha256="0" * 64, structural_summary={"format": "csv"},
        summary_evidence=({"id": "csv:header", "location": "CSV header", "text": "claim_number | policyholder"},),
        targets_truncated=False)
    with TestClient(app) as client:
        stale = client.post(f"/api/documents/{document_id}/versions/{version_id}/ai-summary",
                            headers={"Authorization": f"Bearer {token}"})
    assert stale.status_code == 409
    assert app.state.llm_provider.calls == 5
    app.state.restricted_parser.inspect = lambda *args, **kwargs: SimpleNamespace(
        status="inspected", adapter="csv", sha256=row["sha256"], structural_summary={"format": "csv"},
        summary_evidence=({"id": "csv:header", "location": "CSV header", "text": "claim_number | policyholder"},),
        targets_truncated=False)
    assert [item.status_code for item in additional] == [200, 200, 200, 200, 429]
    assert app.state.llm_provider.calls == 5


def test_ai_summary_cross_tenant_or_missing_version_is_not_found(monkeypatch, tmp_path):
    import packages.api.documents as documents
    settings = _settings(storage_root=str(tmp_path / "storage"))
    app = _test_app(settings)
    tenant_id = uuid4()
    class Engine:
        def connect(self): return nullcontext(object())
        def dispose(self): pass
    class Repo:
        def __init__(self, connection, tenant): pass
        def get_version(self, doc_id, ver_id): return None
    monkeypatch.setattr(documents, "DocumentRepository", Repo)
    monkeypatch.setattr(documents, "api_tenant_transaction", lambda connection, identity: nullcontext(connection))
    app.state.engine = Engine()
    token = create_development_token(settings, subject="processor", tenant_id=tenant_id, role="processor")
    with TestClient(app) as client:
        response = client.post(f"/api/documents/{uuid4()}/versions/{uuid4()}/ai-summary",
                               headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404
