"""Four delivered Phase 3 operations; optional teammate routers live elsewhere."""

from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from starlette.concurrency import run_in_threadpool

from app.api.intake import read_upload
from app.contracts.errors import ProjectException
from app.contracts.models import (
    AuditResponse,
    HealthResponse,
    Identifier,
    Principal,
    ProjectError,
    UploadResponse,
    VersionResponse,
)
from app.contracts.ports import SystemServices
from app.services.projects import ProjectService

ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ProjectError} for status in (400, 401, 403, 404, 413, 415, 422, 500, 503)
}
AUTH: list[dict[str, list[str]]] = [{"BearerAuth": []}]


def bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    parts = value.split()
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None


def create_router(services: SystemServices) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    projects = ProjectService(services)

    def principal(request: Request) -> Principal:
        if services.auth_service is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Authentication unavailable.")
        return services.auth_service.require_user(bearer(request))

    @router.get("/health", response_model=HealthResponse, responses={503: {"model": ProjectError}})
    def health() -> HealthResponse:
        if not (
            services.repository is not None
            and services.repository.health()
            and services.blob_store is not None
            and services.blob_store.health()
            and services.runner is not None
            and services.engine is not None
            and (
                not services.settings.enable_test_interface or services.session_service is not None
            )
        ):
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Service dependencies unavailable.")
        return HealthResponse(
            status="AVAILABLE", apiVersion="1.0", rulesetVersion=services.settings.ruleset_version
        )

    @router.post(
        "/workbooks",
        status_code=202,
        response_model=UploadResponse,
        responses=ERRORS,
        openapi_extra={
            "security": AUTH,
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["file"],
                            "properties": {"file": {"type": "string", "format": "binary"}},
                            "additionalProperties": False,
                        }
                    }
                },
            },
        },
    )
    async def upload(
        request: Request, x_test_session: Annotated[Identifier | None, Header()] = None
    ) -> UploadResponse:
        user = principal(request)
        await run_in_threadpool(projects.check_session, x_test_session, user)
        data, filename = await read_upload(request)
        return await run_in_threadpool(projects.upload, data, filename, user, x_test_session)

    @router.get(
        "/workbooks/{workbookId}/versions/{versionId}",
        response_model=VersionResponse,
        responses=ERRORS,
        openapi_extra={"security": AUTH},
    )
    def version(
        request: Request,
        workbookId: Identifier,
        versionId: Identifier,
        x_test_session: Annotated[Identifier | None, Header()] = None,
    ) -> VersionResponse:
        return projects.get_version(workbookId, versionId, principal(request), x_test_session)

    @router.get(
        "/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}",
        response_model=AuditResponse,
        responses=ERRORS,
        openapi_extra={"security": AUTH},
    )
    def audit(
        request: Request,
        workbookId: Identifier,
        versionId: Identifier,
        auditId: Identifier,
        x_test_session: Annotated[Identifier | None, Header()] = None,
    ) -> AuditResponse:
        return projects.get_audit(
            workbookId, versionId, auditId, principal(request), x_test_session
        )

    return router
