from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .history import HistoryStore
from .models import ConnectionInput, CopyRequest, RollbackRequest
from .prd2_adapter import UnresolvedHookTemplates
from .rossum import RossumError
from .security import new_secret, redact
from .service import EasyPrd2Service

PACKAGE_DIR = Path(__file__).parent


def create_app(*, history: HistoryStore | None = None, service: EasyPrd2Service | None = None) -> FastAPI:
    application_service = service or EasyPrd2Service(history=history)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        application_service.shutdown()

    app = FastAPI(title="Easy PRD2", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = application_service
    app.state.csrf = new_secret()
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":", 1)[0]
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"detail": "Easy PRD2 accepts local requests only"}, status_code=400)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("x-easy-prd2-csrf") != app.state.csrf:
                return JSONResponse({"detail": "Invalid request token"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and url_host(origin) not in {"127.0.0.1", "localhost", "testserver"}:
                return JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(RossumError)
    async def rossum_error(_: Request, exc: RossumError):
        return JSONResponse({"detail": redact(exc, application_service.connections.tokens)}, status_code=400)

    @app.exception_handler(UnresolvedHookTemplates)
    async def hook_template_error(_: Request, exc: UnresolvedHookTemplates):
        return JSONResponse(
            {
                "detail": str(exc),
                "code": "hook_templates_required",
                "context": {"requirements": exc.requirements},
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def safe_error(_: Request, exc: Exception):
        return JSONResponse(
            {"detail": redact(exc, application_service.connections.tokens)},
            status_code=500,
        )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("index.html", {"request": request, "csrf": app.state.csrf})

    @app.post("/api/connections")
    async def connect(payload: ConnectionInput):
        connection = await application_service.connect(payload.api_base, payload.token)
        return {
            "id": connection.id,
            "api_base": connection.api_base,
            "username": connection.user.get("username", ""),
            "user_id": connection.user.get("id"),
        }

    @app.get("/api/connections/{connection_id}/organizations")
    async def organizations(connection_id: str):
        return await application_service.gateway.organizations(application_service.connections.get(connection_id))

    @app.get("/api/connections/{connection_id}/organizations/{organization_id}/workspaces")
    async def workspaces(connection_id: str, organization_id: int):
        connection = await application_service.scoped(connection_id, organization_id)
        return await application_service.gateway.workspaces(connection, organization_id)

    @app.get("/api/connections/{connection_id}/organizations/{organization_id}/admins")
    async def admins(connection_id: str, organization_id: int):
        connection = await application_service.scoped(connection_id, organization_id)
        return await application_service.gateway.admins(connection)

    @app.get("/api/connections/{connection_id}/organizations/{organization_id}/workspaces/{workspace_id}/queues")
    async def queues(connection_id: str, organization_id: int, workspace_id: int):
        connection = await application_service.scoped(connection_id, organization_id)
        return await application_service.gateway.queues(connection, workspace_id)

    @app.post("/api/plans")
    async def create_plan(payload: CopyRequest):
        return await application_service.create_plan(payload)

    @app.post("/api/plans/{plan_id}/execute")
    async def execute_plan(plan_id: str):
        return application_service.start_deploy(plan_id)

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str):
        return application_service.job(job_id)

    @app.get("/api/history")
    async def history():
        return application_service.history.list()

    @app.get("/api/history/{run_id}")
    async def history_item(run_id: str):
        run = application_service.history.get(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        return run

    @app.delete("/api/history/{run_id}")
    async def delete_history(run_id: str):
        application_service.delete_run(run_id)
        return {"ok": True}

    @app.delete("/api/history")
    async def clear_history():
        if any(run.status in {"running", "rolling_back"} for run in application_service.history.list()):
            raise RossumError("Wait for the active operation to finish before clearing history")
        application_service.history.clear()
        return {"ok": True}

    @app.post("/api/history/{run_id}/rollback-plan")
    async def rollback_plan(run_id: str, payload: RollbackRequest):
        return await application_service.rollback_plan(run_id, payload.target_connection_id)

    @app.post("/api/history/{run_id}/rollback")
    async def rollback(run_id: str, payload: RollbackRequest):
        return await application_service.start_rollback(run_id, payload.target_connection_id)

    return app


def url_host(origin: str) -> str:
    from urllib.parse import urlparse

    return urlparse(origin).hostname or ""


app = create_app()
