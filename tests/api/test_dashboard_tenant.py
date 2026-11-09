"""Phase 3A item 3 (N11): the dashboard returns the caller's tenant name from the tenant-scoped row."""
from __future__ import annotations

from contextlib import nullcontext
from uuid import uuid4

from fastapi.testclient import TestClient

from packages.api.auth import create_development_token
from tests.api.test_application import _settings, _test_app


def test_dashboard_reports_the_authenticated_tenant_name(monkeypatch, tmp_path):
    import packages.api.dashboard as dashboard
    settings = _settings(storage_root=str(tmp_path / "storage"))
    app = _test_app(settings)
    tenant_id = uuid4()
    executed: list = []

    class Connection:
        def execute(self, statement, params):
            executed.append((str(statement), params))
            return type("Result", (), {"scalar_one_or_none": staticmethod(lambda: "Channel360 Browser E2E")})()

    class Engine:
        def connect(self): return nullcontext(Connection())
        def dispose(self): pass

    class Repo:
        def __init__(self, connection, tenant):
            assert tenant == tenant_id
        def dashboard_snapshot(self):
            return {"metrics": {"total_documents": 0}, "recent_documents": [], "recent_activity": []}

    monkeypatch.setattr(dashboard, "DocumentRepository", Repo)
    monkeypatch.setattr(dashboard, "api_tenant_transaction", lambda connection, identity: nullcontext(connection))
    app.state.engine = Engine()
    token = create_development_token(settings, subject="owner", tenant_id=tenant_id, role="OWNER")
    with TestClient(app) as client:
        response = client.get("/api/dashboard/summary", headers={"Authorization": f"Bearer {token}"})
        anonymous = client.get("/api/dashboard/summary")
    assert response.status_code == 200
    assert response.json()["tenant"] == {"id": str(tenant_id), "name": "Channel360 Browser E2E"}
    assert executed[0][1] == {"tenant": tenant_id} and "FROM tenants WHERE id=:tenant" in executed[0][0]
    assert anonymous.status_code == 401
