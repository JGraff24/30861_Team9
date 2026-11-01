"""System factory: production adapters, injected owner ports and real project routes."""

import logging
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import JsonValue
from pydantic.json_schema import models_json_schema
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import Response

from app.api.routes import create_router
from app.config import SystemSettings
from app.contracts.errors import IntegrityError, ProjectException, StorageError
from app.contracts.models import ProjectError
from app.contracts.phase4 import (
    AuditReport,
    ReauditRequest,
    RemediationRequest,
)
from app.contracts.ports import (
    AuthService,
    BlobStore,
    EngineRunner,
    Repository,
    SessionService,
    SystemServices,
    WorkbookEngine,
)
from app.services.assembly import assemble, load_factory
from app.services.logging import configure_logging
from app.services.projects import identifier
from app.storage.blobs import FilesystemBlobStore

logger = logging.getLogger(__name__)


def create_app(
    settings: SystemSettings | None = None,
    repository: Repository | None = None,
    blob_store: BlobStore | None = None,
    engine: WorkbookEngine | None = None,
    session_service: SessionService | None = None,
    *,
    runner: EngineRunner | None = None,
    auth_service: AuthService | None = None,
    services: SystemServices | None = None,
) -> FastAPI:
    if services is not None:
        if any(
            value is not None
            for value in (
                settings,
                repository,
                blob_store,
                engine,
                session_service,
                runner,
                auth_service,
            )
        ):
            raise ValueError("inject either SystemServices or individual dependencies")
        container = services
    else:
        container = assemble(
            settings if settings is not None else SystemSettings(),
            repository=repository,
            blob_store=blob_store,
            engine=engine,
            runner=runner,
            session_service=session_service,
            auth_service=auth_service,
        )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        try:
            if container.repository is not None:
                await run_in_threadpool(container.repository.initialize)
                await run_in_threadpool(container.repository.recover_stale_jobs, datetime.now(UTC))
            if isinstance(container.blob_store, FilesystemBlobStore):
                await run_in_threadpool(container.blob_store.initialize)
        except Exception as exc:
            logger.error(
                "storage_initialization_failed", extra={"exceptionType": type(exc).__name__}
            )
        if container.missing_integrations:
            logger.warning(
                "integrations_unavailable",
                extra={
                    "code": ",".join(container.missing_integrations),
                },
            )
        yield

    application = FastAPI(
        title="Team 9 system",
        version="0.4.0",
        description=(
            "Phase 4 backend contracts and durable workbook operations. "
            "Engine capability is checked at runtime."
        ),
        root_path=container.settings.api_root_path,
        lifespan=lifespan,
    )
    application.state.services = container
    application.openapi_version = "3.1.1"

    def error_response(
        request: Request,
        status: int,
        code: str,
        message: str,
        details: dict[str, JsonValue] | None = None,
    ) -> JSONResponse:
        if request.url.path.startswith(("/test/", container.settings.api_root_path + "/test/")):
            payload: dict[str, object] = {"message": message, "source": "system"}
        else:
            payload = ProjectError(
                code=code,
                message=message,
                requestId=request.state.request_id,
                details=details or {},
            ).model_dump(mode="json")
        return JSONResponse(
            payload,
            status_code=status,
            headers={"WWW-Authenticate": "Bearer"} if status == 401 else None,
        )

    @application.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = identifier()
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error(
                "request_fault",
                extra={
                    "requestId": request.state.request_id,
                    "exceptionType": type(exc).__name__,
                    "frames": [
                        f"{frame.name}:{frame.lineno}"
                        for frame in traceback.extract_tb(exc.__traceback__)
                    ],
                },
            )
            response = error_response(
                request, 500, "INTERNAL_ERROR", "Request could not be completed."
            )
        response.headers["X-Request-ID"] = request.state.request_id
        logger.info(
            "request_finished",
            extra={
                "requestId": request.state.request_id,
                "status": response.status_code,
                "duration": round(time.monotonic() - started, 3),
            },
        )
        return response

    @application.exception_handler(ProjectException)
    async def project_error(request: Request, exc: ProjectException) -> JSONResponse:
        logger.warning(
            "project_error",
            extra={
                "requestId": request.state.request_id,
                "code": exc.code,
                "status": exc.status_code,
            },
        )
        return error_response(request, exc.status_code, exc.code, exc.message, exc.details)

    @application.exception_handler(StorageError)
    async def storage_error(request: Request, exc: StorageError) -> JSONResponse:
        code = "SOURCE_INTEGRITY_FAILED" if isinstance(exc, IntegrityError) else "STORAGE_FAILED"
        logger.error(
            "storage_error",
            extra={
                "requestId": request.state.request_id,
                "code": code,
                "exceptionType": type(exc).__name__,
            },
        )
        return error_response(request, 500, code, "Stored resource could not be accessed safely.")

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(request, 400, "INVALID_REQUEST", "Request fields are invalid.")

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return error_response(
            request,
            exc.status_code,
            "NOT_FOUND" if exc.status_code == 404 else "INVALID_REQUEST",
            "Requested operation is unavailable.",
        )

    application.include_router(create_router(container))
    for path in (
        container.settings.test_router_factory
        if container.settings.enable_test_interface
        else None,
        container.settings.ui_router_factory,
    ):
        if path:
            application.include_router(load_factory(path)(container))
    original_openapi = application.openapi

    def openapi() -> dict[str, object]:
        document = original_openapi()
        document.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
        }
        _, catalog = models_json_schema(
            [
                (model, "serialization")
                for model in (AuditReport, ReauditRequest, RemediationRequest)
            ],
            ref_template="#/components/schemas/{model}",
        )
        document["components"].setdefault("schemas", {}).update(catalog["$defs"])
        for path, operations in document.get("paths", {}).items():
            if not path.startswith("/api/v1/"):
                continue
            for method in operations.values():
                if not isinstance(method, dict):
                    continue
                for parameter in method.get("parameters", []):
                    if parameter.get("in") == "path":
                        parameter["schema"].update(
                            minLength=1, maxLength=64, pattern="^[A-Za-z0-9_-]+$"
                        )
                for response in method.get("responses", {}).values():
                    response.setdefault("headers", {})["X-Request-ID"] = {
                        "description": "Server-generated request correlation identifier.",
                        "schema": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                            "pattern": "^[A-Za-z0-9_-]+$",
                        },
                    }
        document["paths"]["/api/v1/health"]["get"]["security"] = []
        document["x-operation-status"] = {
            "delivered-backend-routes": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, "S", "V", "R"],
            "pending-business-operations": [11, 12, 13, 14],
            "integration-note": (
                "Health checks storage and configured ruleset capability. "
                "Phase 4 audit/correction execution requires the Phase 4 engine port."
            ),
        }
        document["x-specification-version"] = "2.1.0"
        document["x-target-ruleset-version"] = "2.0.0"
        return document

    application.openapi = openapi  # type: ignore[method-assign]
    return application
