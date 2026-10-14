"""Separately runnable tester foundation; no test suite/session endpoints yet."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from tester.config import TesterSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class TesterServices:
    settings: TesterSettings


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

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        logger.warning("Foundation only: tester sessions, HTTP suite and reports are pending")
        yield

    application = FastAPI(
        title="Team 9 tester foundation",
        version="0.1.0",
        description="Bootstrap only. Setup, run and report endpoints pending.",
        root_path=container.settings.tester_root_path,
        lifespan=lifespan,
    )
    application.state.services = container
    application.openapi_version = "3.1.1"
    return application
