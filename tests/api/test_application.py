from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr, ValidationError

from packages.api.app import create_app
from packages.api.auth import AuthenticatedIdentity, create_development_token, require_identity
from packages.api.errors import APIError
from packages.api.settings import Settings
from packages.api.documents import _typed_correction


class _ValidationBody(BaseModel):
    value: int


def _settings(**overrides) -> Settings:
    values = {
        "environment": "test",
        "auth_mode": "development",
        "development_auth_secret": SecretStr("unit-test-auth-secret-that-is-at-least-32-bytes"),
        "database_url": "postgresql+psycopg://idw_test:unused@127.0.0.1:55439/idw_test",
        "queue_url": "redis://127.0.0.1:6379/0",
    }
    values.update(overrides)
    return Settings(**values)


def _test_app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/test/identity")
    def identity_route(identity: AuthenticatedIdentity = Depends(require_identity)):
        return identity.model_dump(mode="json")

    @app.post("/test/validation")
    def validation_route(payload: _ValidationBody):
        return payload

    @app.get("/test/failure")
    def failure_route():
        raise APIError(409, "test_conflict", "The requested operation conflicts.", details=[])

    return app


def test_docx_verification_profile_defaults_to_structural_and_rejects_unknown_modes():
    assert _settings().channel360_docx_verification_mode == "structural"
    assert _settings(channel360_docx_verification_mode="structural_and_visual").channel360_docx_verification_mode == "structural_and_visual"
    with pytest.raises(ValidationError):
        _settings(channel360_docx_verification_mode="skip_visual")


def test_live_health_is_available_without_database():
    with TestClient(_test_app(_settings())) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_fails_closed_when_postgres_is_unavailable():
    with TestClient(_test_app(_settings())) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": {"postgres": "unavailable"},
    }


def test_readiness_requires_a_separate_worker_database_credential(tmp_path):
    from contextlib import nullcontext

    settings = _settings(storage_root=str(tmp_path / "storage"))
    app = _test_app(settings)
    app.state.engine.dispose()
    class ReadyConnection:
        def execute(self, statement):
            return 1

    class ReadyEngine:
        def connect(self):
            return nullcontext(ReadyConnection())

        def dispose(self):
            pass

    engine = ReadyEngine()
    app.state.engine = engine
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "components": {"postgres": "ready", "worker_database": "unconfigured"}}


def test_authenticated_tenant_comes_from_signed_token_not_client_header():
    settings = _settings()
    tenant_id = uuid4()
    token = create_development_token(settings, subject="dev-user", tenant_id=tenant_id, role="OWNER")
    with TestClient(_test_app(settings)) as client:
        response = client.get(
            "/test/identity",
            headers={"Authorization": f"Bearer {token}", "X-Tenant-ID": str(uuid4())},
        )
    assert response.status_code == 200
    assert response.json() == {
        "subject": "dev-user",
        "tenant_id": str(tenant_id),
        "role": "OWNER",
    }


def test_client_tenant_header_without_signed_identity_is_unauthorized():
    with TestClient(_test_app(_settings())) as client:
        response = client.get("/test/identity", headers={"X-Tenant-ID": str(uuid4())})
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"


def test_derived_download_route_requires_authenticated_tenant():
    with TestClient(_test_app(_settings())) as client:
        response = client.get(f"/api/documents/{uuid4()}/versions/{uuid4()}/download")
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"


def test_edit_set_history_route_requires_authenticated_tenant():
    with TestClient(_test_app(_settings())) as client:
        response = client.get(f"/api/documents/{uuid4()}/edit-sets")
    assert response.status_code == 401


def test_document_audit_route_requires_authenticated_tenant():
    with TestClient(_test_app(_settings())) as client:
        response = client.get(f"/api/documents/{uuid4()}/audit")
    assert response.status_code == 401


def test_dashboard_summary_requires_authenticated_tenant():
    with TestClient(_test_app(_settings())) as client:
        response = client.get("/api/dashboard/summary")
    assert response.status_code == 401


def test_local_demo_identity_is_issued_only_when_explicitly_enabled():
    tenant_id = uuid4()
    settings = _settings(environment="local", local_demo_auth_enabled=True, local_demo_tenant_id=tenant_id)
    with TestClient(_test_app(settings), client=("127.0.0.1", 55440)) as client:
        session = client.post("/api/local-demo/session", json={})
        assert session.status_code == 200
        body = session.json()
        assert body["role"] == "OWNER"
        assert body["tenant_id"] == str(tenant_id)
        assert body["expires_in"] <= 3600
        identity = client.get("/test/identity", headers={"Authorization": f"Bearer {body['access_token']}"})
        assert identity.status_code == 200
        assert identity.json() == {"subject": "channel360-demo-owner", "tenant_id": str(tenant_id), "role": "OWNER"}


def test_local_demo_identity_endpoint_is_unavailable_by_default():
    with TestClient(_test_app(_settings()), client=("127.0.0.1", 55440)) as client:
        response = client.post("/api/local-demo/session", json={})
    assert response.status_code == 404


def test_local_demo_identity_rejects_unconfigured_roles():
    settings = _settings(environment="local", local_demo_auth_enabled=True, local_demo_tenant_id=uuid4())
    with TestClient(_test_app(settings), client=("127.0.0.1", 55440)) as client:
        response = client.post("/api/local-demo/session", json={"role": "administrator"})
    assert response.status_code == 422


def test_local_demo_identity_is_loopback_only():
    settings = _settings(environment="local", local_demo_auth_enabled=True, local_demo_tenant_id=uuid4())
    with TestClient(_test_app(settings), client=("192.0.2.10", 55440)) as client:
        response = client.post("/api/local-demo/session", json={})
    assert response.status_code == 404


def test_local_demo_identity_requires_explicit_local_environment_and_tenant():
    with pytest.raises(ValidationError):
        _settings(environment="test", local_demo_auth_enabled=True, local_demo_tenant_id=uuid4())


def test_invalid_signature_and_unknown_role_are_rejected():
    settings = _settings()
    valid = create_development_token(settings, subject="dev-user", tenant_id=uuid4(), role="OWNER")
    forged = valid[:-1] + ("a" if valid[-1] != "a" else "b")
    with TestClient(_test_app(settings)) as client:
        invalid_signature = client.get("/test/identity", headers={"Authorization": f"Bearer {forged}"})
        legacy_role_token = create_development_token(
            settings, subject="dev-user", tenant_id=uuid4(), role="processor"
        )
        unknown_role = client.get("/test/identity", headers={"Authorization": f"Bearer {legacy_role_token}"})
    assert invalid_signature.status_code == 401
    assert unknown_role.status_code == 401


def test_development_auth_cannot_be_configured_for_production():
    with pytest.raises(ValidationError):
        _settings(environment="production", auth_mode="development")


def test_api_errors_use_stable_contract_and_validation_does_not_echo_input():
    with TestClient(_test_app(_settings())) as client:
        conflict = client.get("/test/failure")
        invalid = client.post("/test/validation", json={"token": "sensitive-document-content"})
    assert conflict.status_code == 409
    assert conflict.json() == {
        "code": "test_conflict",
        "message": "The requested operation conflicts.",
        "details": [],
    }
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "request_validation_failed"
    assert "sensitive-document-content" not in invalid.text


def test_production_oidc_settings_require_issuer_audience_and_jwks():
    with pytest.raises(ValidationError):
        Settings(environment="production", auth_mode="oidc")
    settings = Settings(
        environment="production",
        auth_mode="oidc",
        oidc_issuer="https://identity.example.test/",
        oidc_audience="insurance-workbench",
        oidc_jwks_url="https://identity.example.test/.well-known/jwks.json",
        database_url="postgresql+psycopg://runtime:secret@127.0.0.1/workbench",
        queue_url="redis://127.0.0.1:6379/0",
    )
    assert settings.auth_mode == "oidc"


def test_database_and_queue_urls_require_supported_backends():
    with pytest.raises(ValidationError):
        _settings(database_url="sqlite:///not-allowed.db")
    with pytest.raises(ValidationError):
        _settings(queue_url="amqp://not-redis")
    with pytest.raises(ValidationError):
        _settings(worker_database_url="sqlite:///forbidden.db")


def test_decimal_and_large_integer_data_corrections_keep_exact_values():
    from decimal import Decimal

    integer = _typed_correction({"type": "integer", "value": "9007199254740993"})
    decimal = _typed_correction({"type": "decimal", "value": "1000000000000000.01"})
    assert integer.value == 9007199254740993
    assert decimal.value == Decimal("1000000000000000.01")


def test_error_and_request_logs_do_not_include_document_content_or_credentials(caplog):
    settings = _settings()
    token = create_development_token(settings, subject="dev-user", tenant_id=uuid4(), role="OWNER")
    with TestClient(_test_app(settings)) as client:
        client.post(
            "/test/validation?access_token=query-secret",
            json={"token": "document-body-secret"},
            headers={"Authorization": f"Bearer {token}"},
        )
    rendered = caplog.text
    assert "query-secret" not in rendered
    assert "document-body-secret" not in rendered
    assert token not in rendered


def test_document_upload_requires_signed_identity_even_with_tenant_header(tmp_path):
    settings = _settings(storage_root=str(tmp_path / "storage"))
    with TestClient(_test_app(settings)) as client:
        response = client.put(
            "/api/documents?filename=policy.xlsx",
            content=b"not trusted",
            headers={"X-Tenant-ID": str(uuid4())},
        )
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"


def test_document_inspection_requires_signed_identity(tmp_path):
    with TestClient(_test_app(_settings(storage_root=str(tmp_path / "storage")))) as client:
        response = client.post(f"/api/documents/{uuid4()}/inspect", headers={"X-Tenant-ID": str(uuid4())})
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"


def test_manual_field_and_data_correction_routes_require_identity(tmp_path):
    with TestClient(_test_app(_settings(storage_root=str(tmp_path / "storage")))) as client:
        document_id, field_id = uuid4(), uuid4()
        field = client.post(f"/api/documents/{document_id}/fields", json={"target_id": "A1", "fingerprint": "f" * 64})
        correction = client.post(f"/api/documents/{document_id}/fields/{field_id}/corrections",
                                 json={"corrected_value": {"type": "string", "value": "new"}, "reason": "Verified by adjuster"})
        listing = client.get(f"/api/documents/{document_id}/corrections/export")
    assert field.status_code == correction.status_code == listing.status_code == 401


def test_legacy_role_token_is_not_an_owner_identity(tmp_path):
    settings = _settings(storage_root=str(tmp_path / "storage"))
    token = create_development_token(settings, subject="reviewer", tenant_id=uuid4(), role="reviewer")
    with TestClient(_test_app(settings)) as client:
        response = client.post(f"/api/documents/{uuid4()}/fields", json={"target_id": "csv:2:1", "fingerprint": "a" * 64},
                               headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_identity"


def test_upload_limit_is_enforced_before_ingestion(tmp_path):
    settings = _settings(storage_root=str(tmp_path / "storage"), max_upload_bytes=2)
    token = create_development_token(settings, subject="dev-user", tenant_id=uuid4(), role="OWNER")
    with TestClient(_test_app(settings)) as client:
        response = client.put(
            "/api/documents?filename=policy.xlsx",
            content=b"123",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 413
    assert response.json()["code"] == "upload_too_large"
    assert not (tmp_path / "storage" / ".staging").exists()


def test_upload_filename_is_reduced_to_a_basename_before_storage(tmp_path):
    settings = _settings(storage_root=str(tmp_path / "storage"))
    token = create_development_token(settings, subject="dev-user", tenant_id=uuid4(), role="OWNER")
    with TestClient(_test_app(settings)) as client:
        response = client.put(
            "/api/documents?filename=..%2Fsecret%2Fpolicy.xlsx",
            content=b"%PDF-1.7 example",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/pdf"},
        )
    # The configured scanner is unavailable in this unit test. Fail-closed response
    # verifies that no filename path is exposed or treated as a successful upload.
    assert response.status_code == 503
    assert response.json()["code"] == "scan_not_verified"
    staged = list((tmp_path / "storage" / ".staging").glob("*/*.part"))
    assert len(staged) == 1
    assert "secret" not in str(staged[0])


def test_frontend_origin_preflight_is_explicit_and_does_not_allow_credentials():
    with TestClient(_test_app(_settings())) as client:
        allowed = client.options(
            "/api/documents",
            headers={"Origin": "http://127.0.0.1:5174", "Access-Control-Request-Method": "PUT",
                     "Access-Control-Request-Headers": "authorization,content-type"},
        )
        denied = client.options(
            "/api/documents",
            headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "PUT"},
        )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:5174"
    assert "access-control-allow-credentials" not in allowed.headers
    assert "access-control-allow-origin" not in denied.headers
