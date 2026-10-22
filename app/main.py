"""ResearchForge application entry point.

Run locally with::

    python -m app.main

or::

    uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import argparse
import logging
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router as api_router
from .config import PROJECT_ROOT, get_settings
from .db import get_db
from .logging_utils import configure_logging, get_logger
from .services import store
from .services.jobs import get_job_runner
from .services.orchestration import ResearchRunner  # noqa: F401  (imported for availability checks)
from .sources import get_registry

log = get_logger("main")

STATIC_DIR = PROJECT_ROOT / "app" / "static"


def create_app() -> FastAPI:
    configure_logging(logging.INFO)
    settings = get_settings()
    settings.ensure_dirs()

    # Any job left running by a previous process is marked interrupted rather
    # than silently reported as still running.
    requeued = store.requeue_stuck_jobs()
    if requeued:
        log.warning("interrupted_jobs_marked", count=requeued)

    application = FastAPI(
        title="ResearchForge",
        version="1.0.0",
        description=(
            "A verified global research and dataset intelligence platform. It discovers real sources, "
            "verifies them, records an audit trail, and refuses to report findings it cannot support."
        ),
    )
    application.include_router(api_router)

    @application.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "status": exc.status_code,
                    "message": exc.detail,
                    "path": str(request.url.path),
                }
            },
        )

    @application.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "status": 422,
                    "message": "the request payload is not valid",
                    "path": str(request.url.path),
                    "details": exc.errors(),
                }
            },
        )

    @application.on_event("startup")
    async def _startup() -> None:
        get_db()
        registry = get_registry()
        log.info(
            "startup",
            adapters=[adapter.name for adapter in registry.all()],
            unavailable=registry.unavailable(),
            db=str(settings.db_path),
        )

    @application.on_event("shutdown")
    async def _shutdown() -> None:
        get_job_runner().shutdown(wait=False)

    if STATIC_DIR.is_dir():
        application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @application.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return application


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ResearchForge server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    url = f"http://{host}:{port}/"

    if not args.no_browser and settings.open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # a headless machine must still start
            pass

    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port, log_level="info", reload=False)


if __name__ == "__main__":
    main()
