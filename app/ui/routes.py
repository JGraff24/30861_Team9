"""Small same-origin UI; application assembly remains Jackson's responsibility."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.api.routes import bearer
from app.contracts.errors import ProjectException
from app.contracts.ports import SystemServices

ROOT = Path(__file__).resolve().parent


def create_router(services: SystemServices) -> APIRouter:
    router = APIRouter()
    templates = Jinja2Templates(directory=ROOT / "templates")

    @router.get("/ui/identity", include_in_schema=False)
    def identity(request: Request) -> JSONResponse:
        """UI-only role discovery; resource authorization remains in the business API."""
        if services.auth_service is None:
            raise ProjectException(503, "SERVICE_UNAVAILABLE", "Authentication is unavailable.")
        principal = services.auth_service.require_user(bearer(request))
        return JSONResponse({"role": principal.role.value}, headers={"Cache-Control": "no-store"})

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "base": request.scope.get("root_path", "").rstrip("/"),
            },
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            },
        )

    @router.get("/ui/app.js", include_in_schema=False)
    def script() -> FileResponse:
        return FileResponse(ROOT / "static" / "app.js", media_type="text/javascript")

    @router.get("/ui/style.css", include_in_schema=False)
    def stylesheet() -> FileResponse:
        return FileResponse(ROOT / "static" / "style.css", media_type="text/css")

    return router
