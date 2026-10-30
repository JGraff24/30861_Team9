"""Phase 4 business HTTP boundary, with authorization before request semantics."""

import json
import re
import unicodedata
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app.api.intake import read_upload
from app.contracts.errors import ProjectException
from app.contracts.models import (
    AuditResponse,
    HealthResponse,
    Identifier,
    Principal,
    ProjectError,
    Role,
    UploadResponse,
    VersionResponse,
)
from app.contracts.phase4 import (
    HistoryResponse,
    ReauditRequest,
    ReauditResponse,
    RemediationAccepted,
    RemediationRequest,
    RemediationResponse,
    ReviewResponse,
)
from app.contracts.ports import SystemServices
from app.services.projects import ProjectService

MAX_JSON_REQUEST_BYTES = 1_048_576
MAX_JSON_RESPONSE_BYTES = 67_108_864
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ProjectError} for status in (400, 401, 403, 404, 409, 413, 415, 422, 500, 503)
}
AUTH: list[dict[str, list[str]]] = [{"BearerAuth": []}]
IDENTIFIER = TypeAdapter(Identifier)
SESSION_PARAMETER: dict[str, Any] = {
    "name": "X-Test-Session",
    "in": "header",
    "required": False,
    "schema": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[A-Za-z0-9_-]+$"},
    "description": "Isolated test namespace; omission selects ordinary data.",
}
MULTIPART_BODY: dict[str, Any] = {
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
}


def bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    parts = value.split()
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None


def _identifier(value: str) -> str:
    try:
        return IDENTIFIER.validate_python(value)
    except ValidationError as exc:
        raise ProjectException(400, "INVALID_REQUEST", "Resource identifier is invalid.") from exc


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    raise ValueError("Non-JSON numeric constant")


async def read_json(request: Request, *, allow_absent: bool = False) -> object:
    """Read at most 1 MiB without coercing, accepting invalid UTF-8 or duplicate keys."""
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_JSON_REQUEST_BYTES:
            raise ProjectException(413, "REQUEST_TOO_LARGE", "JSON request exceeds 1 MiB.")
        data.extend(chunk)
    if not data:
        if allow_absent:
            return {}
        raise ProjectException(400, "INVALID_REQUEST", "A JSON request body is required.")
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise ProjectException(400, "INVALID_REQUEST", "A JSON request body is required.")
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_invalid_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProjectException(400, "INVALID_REQUEST", "Request JSON is malformed.") from exc


def json_response(model: BaseModel, status: int = 200) -> Response:
    data = model.model_dump_json().encode("utf-8")
    if len(data) > MAX_JSON_RESPONSE_BYTES:
        raise ProjectException(500, "RESULT_TOO_LARGE", "Serialized result exceeds its limit.")
    return Response(data, status_code=status, media_type="application/json")


def attachment(data: bytes, filename: str, media_type: str) -> Response:
    # The display name never controls a storage path or an HTTP header delimiter.
    clean = "".join(c for c in filename if unicodedata.category(c) != "Cc")
    fallback = unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode("ascii")
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", fallback).strip(" .") or "download"
    disposition = f'attachment; filename="{fallback}"'
    if clean != fallback:
        disposition += "; filename*=UTF-8''" + quote(clean, safe="")
    return Response(
        data,
        media_type=media_type,
        headers={
            "Content-Length": str(len(data)),
            "Content-Disposition": disposition,
            "Cache-Control": "no-store",
        },
    )


def request_definition(model: type[BaseModel], *, required: bool = True) -> dict[str, Any]:
    return {
        "required": required,
        "content": {
            "application/json": {"schema": {"$ref": f"#/components/schemas/{model.__name__}"}}
        },
    }


def operation(*, body: dict[str, Any] | None = None, key: bool = False) -> dict[str, Any]:
    parameters = [SESSION_PARAMETER]
    if key:
        parameters = [
            *parameters,
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": "^[A-Za-z0-9_-]+$",
                },
            },
        ]
    extra: dict[str, Any] = {"security": AUTH, "parameters": parameters}
    if body is not None:
        extra["requestBody"] = body
    return extra


def download_responses(media_type: str, schema: dict[str, Any]) -> dict[int | str, dict[str, Any]]:
    return {
        **ERRORS,
        200: {
            "description": "Authorized immutable attachment, pre-read and integrity checked.",
            "content": {media_type: {"schema": schema}},
            "headers": {
                "Content-Length": {"schema": {"type": "integer", "minimum": 0}},
                "Content-Disposition": {"schema": {"type": "string"}},
                "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
            },
        },
    }


def create_router(services: SystemServices) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    projects = ProjectService(services)

    def context(request: Request, *ids: str) -> tuple[Principal, str | None]:
        if services.auth_service is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Authentication unavailable.")
        user = services.auth_service.require_user(bearer(request))
        raw_session = request.headers.get("x-test-session")
        session = _identifier(raw_session) if raw_session is not None else None
        projects.check_session(session, user)
        for value in ids:
            _identifier(value)
        return user, session

    @router.get("/health", response_model=HealthResponse, responses={503: {"model": ProjectError}})
    def health() -> HealthResponse:
        engine_rulesets = getattr(services.engine, "supported_rulesets", ("1.0.0",))
        if not (
            services.repository is not None
            and services.repository.health()
            and services.blob_store is not None
            and services.blob_store.health()
            and services.runner is not None
            and services.auth_service is not None
            and services.engine is not None
            and services.settings.ruleset_version in engine_rulesets
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
        openapi_extra=operation(body=MULTIPART_BODY),
    )
    async def upload(request: Request) -> Response:
        user, session = await run_in_threadpool(context, request)
        if user.role != Role.USER:
            raise ProjectException(403, "FORBIDDEN", "A user identity is required for uploads.")
        data, filename = await read_upload(request)
        result = await run_in_threadpool(projects.upload, data, filename, user, session)
        return json_response(result, 202)

    @router.post(
        "/workbooks/{workbookId}/versions",
        status_code=202,
        response_model=UploadResponse,
        responses=ERRORS,
        openapi_extra=operation(body=MULTIPART_BODY),
    )
    async def upload_version(request: Request, workbookId: str) -> Response:
        user, session = await run_in_threadpool(context, request, workbookId)
        await run_in_threadpool(projects.authorize_workbook, workbookId, user, session, True)
        data, filename = await read_upload(request)
        result = await run_in_threadpool(projects.upload, data, filename, user, session, workbookId)
        return json_response(result, 202)

    @router.get(
        "/workbooks/{workbookId}/versions/{versionId}",
        response_model=VersionResponse,
        responses=ERRORS,
        openapi_extra=operation(),
    )
    def version(request: Request, workbookId: str, versionId: str) -> Response:
        user, session = context(request, workbookId, versionId)
        return json_response(projects.get_version(workbookId, versionId, user, session))

    @router.get(
        "/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}",
        response_model=AuditResponse,
        responses=ERRORS,
        openapi_extra=operation(),
    )
    def audit(request: Request, workbookId: str, versionId: str, auditId: str) -> Response:
        user, session = context(request, workbookId, versionId, auditId)
        return json_response(projects.get_audit(workbookId, versionId, auditId, user, session))

    @router.post(
        "/workbooks/{workbookId}/versions/{versionId}/audits",
        status_code=202,
        response_model=ReauditResponse,
        responses=ERRORS,
        openapi_extra=operation(body=request_definition(ReauditRequest, required=False)),
    )
    async def reaudit(request: Request, workbookId: str, versionId: str) -> Response:
        user, session = await run_in_threadpool(context, request, workbookId, versionId)
        await run_in_threadpool(
            projects.authorize_version, workbookId, versionId, user, session, True
        )
        await run_in_threadpool(projects.check_operation, workbookId, versionId, user, session)
        body = await read_json(request, allow_absent=True)
        result = await run_in_threadpool(
            projects.reaudit, workbookId, versionId, user, session, body
        )
        return json_response(result, 202)

    @router.get(
        "/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}/review",
        response_model=ReviewResponse,
        responses=ERRORS,
        openapi_extra=operation(),
    )
    def review(request: Request, workbookId: str, versionId: str, auditId: str) -> Response:
        user, session = context(request, workbookId, versionId, auditId)
        return json_response(projects.get_review(workbookId, versionId, auditId, user, session))

    @router.post(
        "/workbooks/{workbookId}/versions/{versionId}/remediations",
        status_code=202,
        response_model=RemediationAccepted,
        responses=ERRORS,
        openapi_extra=operation(body=request_definition(RemediationRequest), key=True),
    )
    async def submit_remediation(request: Request, workbookId: str, versionId: str) -> Response:
        user, session = await run_in_threadpool(context, request, workbookId, versionId)
        await run_in_threadpool(
            projects.authorize_version, workbookId, versionId, user, session, True
        )
        body = await read_json(request)
        result = await run_in_threadpool(
            projects.submit_remediation,
            workbookId,
            versionId,
            user,
            session,
            request.headers.get("idempotency-key"),
            body,
        )
        return json_response(result, 202)

    @router.get(
        "/remediations/{remediationId}",
        response_model=RemediationResponse,
        responses=ERRORS,
        openapi_extra=operation(),
    )
    def remediation(request: Request, remediationId: str) -> Response:
        user, session = context(request, remediationId)
        return json_response(projects.get_remediation(remediationId, user, session))

    @router.get(
        "/workbooks/{workbookId}/history",
        response_model=HistoryResponse,
        responses=ERRORS,
        openapi_extra=operation(),
    )
    def history(request: Request, workbookId: str) -> Response:
        user, session = context(request, workbookId)
        return json_response(projects.get_history(workbookId, user, session))

    @router.get(
        "/workbooks/{workbookId}/versions/{versionId}/source",
        response_class=Response,
        responses=download_responses(XLSX_MEDIA_TYPE, {"type": "string", "format": "binary"}),
        openapi_extra=operation(),
    )
    def source(request: Request, workbookId: str, versionId: str) -> Response:
        user, session = context(request, workbookId, versionId)
        data, name = projects.get_source(workbookId, versionId, user, session)
        return attachment(data, name, XLSX_MEDIA_TYPE)

    @router.get(
        "/outputs/{outputId}/workbook",
        response_class=Response,
        responses=download_responses(XLSX_MEDIA_TYPE, {"type": "string", "format": "binary"}),
        openapi_extra=operation(),
    )
    def output(request: Request, outputId: str) -> Response:
        user, session = context(request, outputId)
        data, name = projects.get_output(outputId, user, session)
        return attachment(data, name, XLSX_MEDIA_TYPE)

    @router.get(
        "/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}/report",
        response_class=Response,
        responses=download_responses(
            "application/json", {"$ref": "#/components/schemas/AuditReport"}
        ),
        openapi_extra=operation(),
    )
    def report(request: Request, workbookId: str, versionId: str, auditId: str) -> Response:
        user, session = context(request, workbookId, versionId, auditId)
        data, name = projects.get_report(workbookId, versionId, auditId, user, session)
        if len(data) > MAX_JSON_RESPONSE_BYTES:
            raise ProjectException(500, "RESULT_TOO_LARGE", "Serialized report exceeds its limit.")
        return attachment(data, name, "application/json")

    return router
