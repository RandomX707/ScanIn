from __future__ import annotations

import hashlib
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from packages.api.auth import create_development_token
from tests.api.test_application import _settings, _test_app


def _target(target_id: str, value: str = "Approved") -> dict:
    return {"id": target_id, "sheet": "Claims", "cell": target_id.split("!")[-1], "label": target_id,
            "value": {"type": "string", "value": value, "native": value, "display": None},
            "fingerprint": hashlib.sha256(target_id.encode()).hexdigest(), "editable": True, "reason": None,
            "dependents": []}


def _app(monkeypatch, tmp_path):
    import packages.api.documents as documents
    settings = _settings(storage_root=str(tmp_path / "storage"))
    app = _test_app(settings)
    tenant_id, document_id, version_id, edit_set_id = uuid4(), uuid4(), uuid4(), uuid4()
    key = f"original/{tenant_id}/{document_id}/claims.xlsx"
    path = app.state.document_storage.root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    content = b"synthetic workbook bytes"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    row = {"ingestion_state": "ready", "kind": "original", "filename": "claims.xlsx", "storage_key": key,
           "size_bytes": len(content), "sha256": digest, "parent_version_id": None,
           "current_version_id": version_id, "tenant_id": tenant_id}
    saved: list = []

    class Engine:
        def connect(self): return nullcontext(object())
        def dispose(self): pass

    class Repo:
        def __init__(self, connection, tenant):
            assert tenant == tenant_id
        def get(self, doc_id):
            return row if doc_id == document_id else None
        def get_version(self, doc_id, ver_id):
            return row if (doc_id, ver_id) == (document_id, version_id) else None
        def get_edit_set(self, es_id):
            return {"document_id": document_id, "status": "draft", "created_by": "owner",
                    "base_version_id": version_id, "current_version_id": version_id,
                    "filename": "claims.xlsx"} if es_id == edit_set_id else None
        def add_patches(self, es_id, planned, actor_id):
            saved.extend(planned)

    calls: list[dict] = []

    def inspect(*args, **kwargs):
        calls.append(kwargs)
        ids = kwargs.get("target_ids")
        if ids is not None:
            targets = tuple(_target(item) for item in ids if item.startswith("xlsx:Claims!"))
            page = {"mode": "lookup", "offset": 0, "limit": 2000, "total": 2400, "found": len(targets),
                    "requested": len(ids), "next_offset": None}
            truncated = False
        else:
            offset = kwargs.get("target_offset", 0)
            count = max(0, min(2000, 2400 - offset))
            targets = tuple(_target(f"xlsx:Claims!A{offset + index + 1}") for index in range(count))
            page = {"mode": "page", "offset": offset, "limit": 2000, "total": 2400,
                    "next_offset": offset + count if offset + count < 2400 else None}
            truncated = True
        return SimpleNamespace(status="inspected", adapter="xlsx", sha256=digest, media_type="application/xlsx",
                               capability={"level": "editable", "reasons": []}, features=(), targets=targets,
                               targets_truncated=truncated, targets_page=page, details=None,
                               structural_summary=None)

    monkeypatch.setattr(documents, "DocumentRepository", Repo)
    monkeypatch.setattr(documents, "api_tenant_transaction", lambda connection, identity: nullcontext(connection))
    app.state.engine = Engine()
    app.state.restricted_parser = SimpleNamespace(inspect=inspect)
    token = create_development_token(settings, subject="owner", tenant_id=tenant_id, role="OWNER")
    return app, {"Authorization": f"Bearer {token}"}, document_id, version_id, edit_set_id, calls, saved


def test_view_pages_large_documents_instead_of_refusing_them(monkeypatch, tmp_path):
    app, headers, document_id, version_id, _, calls, _ = _app(monkeypatch, tmp_path)
    base = f"/api/documents/{document_id}/versions/{version_id}/view"
    with TestClient(app) as client:
        first = client.get(base, headers=headers)
        second = client.get(base, params={"target_offset": 2000}, headers=headers)
        lookup = client.get(base, params=[("target_id", "xlsx:Claims!E105")], headers=headers)
        beyond = client.get(base, params={"target_offset": 2400}, headers=headers)
        too_many = client.get(base, params=[("target_id", f"xlsx:Claims!A{i}") for i in range(51)], headers=headers)
        mixed = client.get(base, params=[("target_offset", "2000"), ("target_id", "xlsx:Claims!A1")], headers=headers)
        negative = client.get(base, params={"target_offset": -1}, headers=headers)
    assert first.status_code == 200, first.text
    assert len(first.json()["targets"]) == 2000 and first.json()["targets_page"]["next_offset"] == 2000
    assert second.status_code == 200 and len(second.json()["targets"]) == 400
    assert second.json()["targets_page"]["next_offset"] is None
    assert lookup.status_code == 200 and [item["id"] for item in lookup.json()["targets"]] == ["xlsx:Claims!E105"]
    assert beyond.status_code == 416 and beyond.json()["code"] == "target_page_out_of_range"
    assert too_many.status_code == 422 and mixed.status_code == 422 and negative.status_code == 422
    assert calls[0]["target_offset"] == 0 and calls[0]["target_ids"] is None
    assert calls[1]["target_offset"] == 2000 and calls[2]["target_ids"] == ["xlsx:Claims!E105"]


def test_patches_resolve_targets_beyond_the_first_page_by_exact_lookup(monkeypatch, tmp_path):
    app, headers, document_id, _, edit_set_id, calls, saved = _app(monkeypatch, tmp_path)
    deep = _target("xlsx:Claims!C1500")
    patch = {"target_id": deep["id"], "fingerprint": deep["fingerprint"],
             "new_value": {"type": "string", "value": "Escalated"}}
    stale = {**patch, "fingerprint": "0" * 64}
    with TestClient(app) as client:
        accepted = client.put(f"/api/documents/{document_id}/edit-sets/{edit_set_id}/patches",
                              json={"patches": [patch]}, headers=headers)
        rejected = client.put(f"/api/documents/{document_id}/edit-sets/{edit_set_id}/patches",
                              json={"patches": [stale]}, headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert calls[0]["target_ids"] == ["xlsx:Claims!C1500"]
    assert saved == [{"target_id": deep["id"], "expected_old_value": deep["value"],
                      "expected_fingerprint": deep["fingerprint"],
                      "new_value": {"type": "string", "value": "Escalated", "native": None, "display": None}}]
    assert rejected.status_code == 409 and rejected.json()["code"] == "target_not_editable_or_stale"
