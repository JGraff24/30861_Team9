"""Independent tester service: instructor setup/run/report endpoints over HTTP only."""

import logging
import socket
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Query, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.contracts.models import (
    Identifier,
    RunRequest,
    SetupResponse,
    SuiteReport,
    TestEndpointError,
    TesterSetupRequest,
)
from tester.auth import operator_subject
from tester.config import TesterSettings
from tester.errors import install_error_handlers
from tester.service import TesterService
from tester.target import Resolver

logger = logging.getLogger(__name__)

ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": TestEndpointError} for status in (400, 401, 403, 404, 409, 500)
}
REPORT: dict[int | str, dict[str, Any]] = {
    200: {"model": SuiteReport, "description": "Stored suite report (byte-identical on retrieval)"},
    **ERRORS,
}
bearer = HTTPBearer(auto_error=False, description="Grader/operator bearer token")


@dataclass(frozen=True, repr=False)
class TesterServices:
    settings: TesterSettings
    # Test hooks only; production uses the real network transport, resolver and clock.
    transport: httpx.BaseTransport | None = None
    resolver: Resolver = field(default=socket.getaddrinfo)
    sleep: Callable[[float], None] = field(default=time.sleep)
    clock: Callable[[], float] = field(default=time.monotonic)


def create_app(
    settings: TesterSettings | None = None,
    *,
    services: TesterServices | None = None,
) -> FastAPI:
    if settings is not None and services is not None:
        raise ValueError("inject either TesterServices or settings")
    container = (
        services
        if services is not None
        else TesterServices(settings=settings if settings is not None else TesterSettings())
    )
    service = TesterService(
        container.settings,
        transport=container.transport,
        resolver=container.resolver,
        sleep=container.sleep,
        clock=container.clock,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # Only touches storage when a previous process already created it.
        await run_in_threadpool(service.recover_interrupted_runs)
        if not container.settings.tester_operator_token_hashes:
            logger.warning("No tester operator credentials configured; all calls return 401")
        yield

    application = FastAPI(
        title="Team 9 tester",
        version="0.2.0",
        description="Instructor cross-team tester: setup, single-use run and stored report.",
        root_path=container.settings.tester_root_path,
        lifespan=lifespan,
    )
    application.state.services = container
    application.state.tester = service
    application.openapi_version = "3.1.1"
    install_error_handlers(application)
    default_openapi = application.openapi

    def openapi() -> dict[str, Any]:
        # Invalid requests are 400 {message, source}; FastAPI's default 422 never occurs here.
        schema = default_openapi()
        for operations in schema["paths"].values():
            for operation in operations.values():
                operation["responses"].pop("422", None)
        for unused in ("HTTPValidationError", "ValidationError"):
            schema.get("components", {}).get("schemas", {}).pop(unused, None)
        return schema

    application.openapi = openapi  # type: ignore[method-assign]

    def operator(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        token = credentials.credentials if credentials is not None else None
        return operator_subject(token, container.settings.tester_operator_token_hashes)

    Operator = Annotated[str, Depends(operator)]

    @application.post("/test/setup", status_code=201, responses=ERRORS)
    def setup(request: TesterSetupRequest, subject: Operator) -> SetupResponse:
        return service.setup(request, subject)

    @application.post("/test/run", responses=REPORT, response_class=JSONResponse)
    def run(request: RunRequest, subject: Operator) -> Response:
        return Response(service.run(request, subject), media_type="application/json")

    @application.get("/test/report", responses=REPORT, response_class=JSONResponse)
    def report(subject: Operator, session_id: Annotated[Identifier, Query()]) -> Response:
        return Response(service.report(session_id, subject), media_type="application/json")

    return application
