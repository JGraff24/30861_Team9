"""Instructor envelopes are handled locally, independently of project handlers."""

import asyncio
from collections.abc import Awaitable, Callable
from json import JSONDecodeError
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from app.contracts.errors import DuplicateIdentifierError, ProjectException, SessionBusyError
from app.contracts.models import (
    Identifier,
    Principal,
    SetupResponse,
    SystemReport,
    SystemSetupRequest,
    TestEndpointError,
)
from app.contracts.ports import SystemServices

SETUP_TIMEOUT_SECONDS = 30
REPORT_TIMEOUT_SECONDS = 10
ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": TestEndpointError} for status in (400, 401, 403, 404, 409, 500, 503)
}
AUTH: dict[str, Any] = {"security": [{"BearerAuth": []}]}


def create_router(services: SystemServices) -> APIRouter:
    router = APIRouter()
    if not services.settings.enable_test_interface:
        return router

    async def handle(
        request: Request, action: Callable[[Principal], Awaitable[JSONResponse]]
    ) -> JSONResponse:
        try:
            if services.auth_service is None or services.session_service is None:
                raise ProjectException(503, "UNAVAILABLE", "Testing services unavailable.")
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            async with asyncio.timeout(
                SETUP_TIMEOUT_SECONDS if request.method == "POST" else REPORT_TIMEOUT_SECONDS
            ):
                principal = await run_in_threadpool(
                    services.auth_service.require_test_admin,
                    token if scheme.lower() == "bearer" and token else None,
                )
                return await action(principal)
        except ProjectException as exc:
            status, message = exc.status_code, exc.message
        except (DuplicateIdentifierError, SessionBusyError):
            status, message = 409, "Session ID is reserved or session has active work."
        except Exception:
            status, message = 500, "Testing operation failed."
        return JSONResponse({"message": message, "source": "system"}, status_code=status)

    @router.post(
        "/test/setup",
        status_code=201,
        response_model=SetupResponse,
        responses=ERRORS,
        openapi_extra=AUTH
        | {
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": SystemSetupRequest.model_json_schema()}},
            }
        },
    )
    async def setup(request: Request) -> JSONResponse:
        async def action(principal: Principal) -> JSONResponse:
            try:
                body = SystemSetupRequest.model_validate(await request.json())
            except (ValidationError, JSONDecodeError, UnicodeDecodeError):
                raise ProjectException(400, "INVALID_REQUEST", "Invalid test request.") from None
            assert services.session_service is not None
            await run_in_threadpool(
                services.session_service.prepare, body.session_id, principal, dict(body.setup)
            )
            return JSONResponse({"session_id": body.session_id}, status_code=201)

        return await handle(request, action)

    @router.get(
        "/test/report",
        response_model=SystemReport,
        responses=ERRORS,
        openapi_extra=AUTH
        | {
            "parameters": [
                {
                    "name": "session_id",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
                }
            ]
        },
    )
    async def report(request: Request) -> JSONResponse:
        async def action(principal: Principal) -> JSONResponse:
            try:
                session_id = TypeAdapter(Identifier).validate_python(
                    request.query_params.get("session_id")
                )
            except ValidationError:
                raise ProjectException(400, "INVALID_REQUEST", "Invalid test request.") from None
            assert services.session_service is not None
            coverage = await run_in_threadpool(
                services.session_service.get_code_coverage, session_id, principal
            )
            return JSONResponse(
                {"session_id": session_id, "coverage": {"code": coverage.model_dump()}}
            )

        return await handle(request, action)

    return router
