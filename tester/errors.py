"""Instructor {message, source} errors; never the project envelope, never secrets."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.contracts.models import ErrorSource, TestEndpointError

logger = logging.getLogger(__name__)


class TesterError(Exception):
    def __init__(self, status_code: int, message: str, source: ErrorSource) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.source = source

    def response(self) -> JSONResponse:
        body = TestEndpointError(message=self.message, source=self.source)
        return JSONResponse(status_code=self.status_code, content=body.model_dump(mode="json"))


def bad_request(message: str) -> TesterError:
    return TesterError(400, message, ErrorSource.TESTER)


def install_error_handlers(application: FastAPI) -> None:
    async def tester_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, TesterError)
        return exc.response()

    async def invalid_request(request: Request, exc: Exception) -> JSONResponse:
        # Raw validation details may echo request input; report only a fixed message.
        return bad_request("Invalid request body or parameters.").response()

    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled tester failure: %s", type(exc).__name__)
        return TesterError(500, "Internal tester failure.", ErrorSource.TESTER).response()

    application.add_exception_handler(TesterError, tester_error)
    application.add_exception_handler(RequestValidationError, invalid_request)
    application.add_exception_handler(Exception, unexpected)
