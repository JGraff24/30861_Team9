"""Runnable foundation factory. No Phase 3 feature routers have been delivered."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import SystemSettings
from app.contracts.ports import (
    AuthService,
    BlobStore,
    EngineRunner,
    Repository,
    SessionService,
    SystemServices,
    WorkbookEngine,
)

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
        container = SystemServices(
            settings=settings if settings is not None else SystemSettings(),
            repository=repository,
            blob_store=blob_store,
            engine=engine,
            session_service=session_service,
            runner=runner,
            auth_service=auth_service,
        )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        logger.warning(
            "Foundation only: feature routers are pending; missing integrations: %s",
            ", ".join(container.missing_integrations) or "none (routers still pending)",
        )
        yield

    application = FastAPI(
        title="Team 9 system foundation",
        version="0.1.0",
        description=(
            "Bootstrap only. Health, upload, version, audit, test interface and UI pending."
        ),
        root_path=container.settings.api_root_path,
        lifespan=lifespan,
    )
    application.state.services = container
    application.openapi_version = "3.1.1"
    return application
