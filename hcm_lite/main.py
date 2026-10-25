"""Application factory. Run locally with:  uvicorn hcm_lite.main:app --reload"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import IntegrityError

from . import __version__
from .ai.service import AIService
from .api.deps import AppState
from .api.routes import ROUTERS
from .clock import Clock, SystemClock
from .config import Settings
from .context import Ctx
from .db import init_db, make_engine, make_session_factory
from .errors import DomainError
from .services import org

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger("hcm_lite")


def create_app(settings: Settings | None = None, *, clock: Clock | None = None,
               ai: AIService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    init_db(engine, auto_migrate=settings.auto_migrate)
    session_factory = make_session_factory(engine)
    clock = clock or SystemClock(settings.timezone)
    state = AppState(settings=settings, engine=engine, session_factory=session_factory, clock=clock,
                     ai=ai or AIService(settings))

    with session_factory() as db:  # first run from env (ADMIN_EMAIL / ADMIN_PASSWORD), if provided
        if org.bootstrap_from_env(Ctx(db=db, clock=clock, settings=settings)):
            db.commit()
            log.info("First-run setup created from environment variables")

    app = FastAPI(title="HCM Lite API", version=__version__,
                  description="AI-assisted Human Capital Management — Saylani Hackathon 2026")
    app.state.hcm = state
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                       allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(DomainError)
    async def _domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        headers = {}
        if isinstance(exc.details, dict) and "retry_after_seconds" in exc.details:
            headers["Retry-After"] = str(exc.details["retry_after_seconds"])
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict(), headers=headers)

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_request: Request, exc: IntegrityError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": "conflict", "message": "Duplicate or conflicting record"})

    @app.exception_handler(PermissionError)
    async def _permission_error(_request: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"error": "forbidden", "message": str(exc)})

    for router in ROUTERS:
        app.include_router(router)

    @app.get("/health", tags=["meta"])
    def health():
        return {"status": "ok", "version": __version__, "ai_provider": state.ai.provider.name}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


_APP: FastAPI | None = None


def __getattr__(name: str):  # pragma: no cover - lets `uvicorn hcm_lite.main:app` build the app lazily
    global _APP
    if name == "app":
        if _APP is None:
            logging.basicConfig(level=logging.INFO)
            _APP = create_app()
        return _APP
    raise AttributeError(name)
