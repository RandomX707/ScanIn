from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from packages.api.errors import APIError, APIErrorResponse
from packages.api.documents import router as documents_router
from packages.api.dashboard import router as dashboard_router
from packages.api.archives import router as archives_router
from packages.api.workspace import router as workspace_router
from packages.api.render import router as render_router
from packages.api.compare import router as compare_router
from packages.api.working_copy import router as working_copy_router
from packages.api.sheet_index import SheetIndexManager, router as sheet_index_router
from packages.workers.xlsx_index import RestrictedXlsxIndexer
from packages.workers.sigcheck import SignatureValidator
from packages.workers.office_render import SandboxedOfficeRenderer
from packages.api.logging_config import RequestLogMiddleware
from packages.api.local_demo import router as local_demo_router
from packages.api.settings import Settings, get_settings
from packages.db.engine import create_runtime_engine, create_worker_engine
from packages.ingestion.pipeline import IngestionOrchestrator
from packages.ingestion.scanner import ClamAvUnixScanner
from packages.storage.local import ImmutableLocalStorage
from packages.workers.pdf_edit import RestrictedPdfEditor
from packages.workers.parser import RestrictedParser


def _validation_details(error: RequestValidationError) -> list[dict[str, Any]]:
    return [
        {"location": list(item.get("loc", ())), "reason": item.get("msg", "invalid value"), "type": item.get("type")}
        for item in error.errors()
    ]


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    engine = create_runtime_engine(runtime_settings)
    worker_engine = create_worker_engine(runtime_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        engine.dispose()
        if worker_engine is not None:
            worker_engine.dispose()

    app = FastAPI(
        title="Insurance Document Workbench API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings
    app.state.ai_summary_requests = {}
    app.state.ai_summary_lock = Lock()
    app.state.engine = engine
    app.state.worker_engine = worker_engine
    app.state.document_storage = ImmutableLocalStorage(runtime_settings.storage_root)
    app.state.restricted_parser = RestrictedParser(Path(__file__).resolve().parents[2])
    app.state.office_renderer = SandboxedOfficeRenderer()
    app.state.signature_validator = SignatureValidator()
    app.state.sheet_index = SheetIndexManager(RestrictedXlsxIndexer(Path(__file__).resolve().parents[2]))
    app.state.pdf_editor = RestrictedPdfEditor(Path(__file__).resolve().parents[2])
    app.state.ingestion_pipeline = IngestionOrchestrator(
        app.state.document_storage,
        ClamAvUnixScanner(runtime_settings.clamav_socket_path),
        max_bytes=runtime_settings.max_upload_bytes,
    )
    app.include_router(documents_router)
    app.include_router(dashboard_router)
    app.include_router(workspace_router)
    app.include_router(render_router)
    app.include_router(compare_router)
    app.include_router(working_copy_router)
    app.include_router(sheet_index_router)
    app.include_router(archives_router)
    app.include_router(local_demo_router)
    app.add_middleware(RequestLogMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.web_origins,
        allow_credentials=False,
        allow_methods=["GET", "PUT", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Content-Disposition"],
    )
    logging.getLogger("idw.api").setLevel(getattr(logging, runtime_settings.log_level))

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, error: APIError) -> JSONResponse:
        payload = APIErrorResponse(code=error.code, message=error.message, details=error.details)
        return JSONResponse(status_code=error.status_code, content=payload.model_dump())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, error: RequestValidationError) -> JSONResponse:
        payload = APIErrorResponse(
            code="request_validation_failed",
            message="The request did not match the required contract.",
            details=_validation_details(error),
        )
        return JSONResponse(status_code=422, content=payload.model_dump())

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, error: StarletteHTTPException) -> JSONResponse:
        status_messages = {
            401: ("authentication_required", "A valid identity is required."),
            403: ("forbidden", "The operation is not permitted."),
            404: ("not_found", "The requested resource was not found."),
        }
        code, message = status_messages.get(error.status_code, ("http_error", "The request could not be completed."))
        payload = APIErrorResponse(code=code, message=message, details=[])
        return JSONResponse(status_code=error.status_code, content=payload.model_dump(), headers=error.headers)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, error: Exception) -> JSONResponse:
        logging.getLogger("idw.api").error("unhandled_error type=%s", type(error).__name__)
        payload = APIErrorResponse(
            code="internal_error",
            message="The request could not be completed.",
            details=[],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    @app.get("/health/live", tags=["health"])
    def health_live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready", tags=["health"])
    def health_ready(request: Request) -> JSONResponse:
        try:
            with request.app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "components": {"postgres": "unavailable"}},
            )
        worker_status = "unconfigured"
        if request.app.state.worker_engine is not None:
            try:
                with request.app.state.worker_engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
                worker_status = "ready"
            except SQLAlchemyError:
                worker_status = "unavailable"
        components = {"postgres": "ready", "worker_database": worker_status}
        status = "ready" if worker_status == "ready" else "not_ready"
        return JSONResponse(status_code=200 if status == "ready" else 503,
                            content={"status": status, "components": components})

    return app
