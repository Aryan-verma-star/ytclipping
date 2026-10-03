"""FastAPI application factory.

Wires: configuration → database → provider chain → style registry →
REST API → rate limiting → static frontend serving → background worker.

Run locally:  cd backend && uvicorn app.main:app --port 8000
Sandbox:      scripts/sandbox_backend.sh (port 8000 behind the Caddy gateway)
Production:   Dockerfile CMD (Render).
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.config import BACKEND_DIR, Settings, get_settings
from app.core.errors import AppError, PayloadTooLargeError, RateLimitError
from app.core.ratelimit import RateLimiter, client_ip
from app.db.base import Database
from app.downloader.registry import build_provider_chain
from app.api import routes_ai, routes_jobs, routes_media, routes_meta, routes_previews, routes_styles
from app.services.preview import PreviewWorker, recover_stale_previews
from app.services.retention import RetentionSweeper
from app.services.worker import JobWorker, recover_stale_jobs

log = logging.getLogger("clipper")

# Media endpoints (clip files, preview video/stream, filmstrip tiles, and
# the vidssave media relay) are exempt from the per-IP request counter: a
# single <video> element legitimately issues dozens of Range requests and a
# filmstrip loads ~60 tiles at once. Abuse on these paths is bounded by
# retention + download caps + the proxy's vidssave-only URL allowlist.
_MEDIA_PATH_RE = re.compile(
    r"^/api/(?:jobs/[^/]+/clip|previews/[^/]+/(?:video|stream|thumbs/[^/]+)|media/proxy)$"
)


def _run_migrations(db: Database) -> bool:
    """Alembic upgrade-to-head; returns False when it had to fall back."""
    try:
        from alembic import command
        from alembic.config import Config as AlembicConfig

        cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        cfg.set_main_option("sqlalchemy.url", db.url)
        command.upgrade(cfg, "head")
        return True
    except Exception:
        log.exception("alembic migration failed — falling back to create_all()")
        db.create_all()
        return False


def _error_body(message: str, code: str, field: str | None = None, details: list | None = None) -> dict:
    err: dict = {"code": code, "message": message}
    if field:
        err["field"] = field
    if details:
        err["details"] = details
    return {"error": err}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings.ensure_dirs()

    db = Database(settings.resolved_database_url)
    providers = build_provider_chain(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop_event = threading.Event()
        worker = None
        preview_worker = None
        sweeper = None
        try:
            if settings.init_db_on_startup:
                if _run_migrations(db):
                    log.info("database schema is up to date (alembic)")
            recover_stale_jobs(db)
            recover_stale_previews(db)
            if settings.worker_enabled:
                worker = JobWorker(db, settings, providers, stop_event)
                worker.start()
                app.state.worker = worker
                preview_worker = PreviewWorker(db, settings, providers, stop_event)
                preview_worker.start()
                app.state.preview_worker = preview_worker
            sweeper = RetentionSweeper(db, settings, stop_event)
            sweeper.start()
            app.state.stop_event = stop_event
            log.info(
                "%s v%s ready (env=%s, providers=%s)",
                settings.app_name,
                __version__,
                settings.environment,
                [p.name for p in providers],
            )
            yield
        finally:
            stop_event.set()
            log.info("shutting down background threads")

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Paste a YouTube URL, choose start/end times and a clip style, "
            "and download the trimmed clip. Personal-scale tool for free-tier "
            "hosting. NOTE: downloading YouTube content via third-party "
            "services may violate YouTube's ToS — you are responsible for "
            "having the rights to the content you clip."
        ),
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.db = db
    app.state.providers = providers
    app.state.worker = None

    # ---------------- error shaping ----------------
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.message, exc.code, exc.field, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(_: Request, exc: RequestValidationError):
        details = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", []))
            if loc.startswith("body."):
                loc = loc[len("body."):]
            details.append({"field": loc, "message": e.get("msg", "invalid value")})
        return JSONResponse(
            status_code=422,
            content=_error_body("Request validation failed.", "validation_error", details=details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(str(exc.detail), f"http_{exc.status_code}"),
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception):
        log.exception("unhandled error: %r", exc)
        return JSONResponse(
            status_code=500,
            content=_error_body("Internal server error — see server logs.", "internal_error"),
        )

    # ---------------- rate limiting + request size guard ----------------
    limiter = RateLimiter(
        settings.rate_limit_per_minute,
        settings.rate_limit_jobs_per_minute,
        settings.rate_limit_previews_per_minute,
    )
    api_prefix = "/api"

    @app.middleware("http")
    async def protections(request: Request, call_next):
        # request-size protection (we only ever submit tiny JSON bodies)
        method = request.method.upper()
        if method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > settings.max_request_bytes:
                return JSONResponse(
                    status_code=413,
                    content=_error_body(
                        f"Request body too large (limit {settings.max_request_bytes} bytes).",
                        "payload_too_large",
                    ),
                )
        # per-IP sliding-window rate limiting (media streaming is exempt — see
        # _MEDIA_PATH_RE: video players and filmstrips make many legitimate GETs)
        ip = client_ip(request.headers, request.client.host if request.client else None)
        path = request.url.path
        if method == "GET" and _MEDIA_PATH_RE.match(path):
            return await call_next(request)
        if method == "POST" and path == f"{api_prefix}/jobs":
            bucket, label = "job", "job creation"
        elif method == "POST" and path == f"{api_prefix}/previews":
            bucket, label = "preview", "preview creation"
        else:
            bucket, label = "general", "requests"
        allowed, retry_after = limiter.check(ip, bucket)
        if not allowed:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content=_error_body(
                    f"Rate limit exceeded ({label}). "
                    f"Retry after {retry_after}s.",
                    "rate_limited",
                ),
            )
        return await call_next(request)

    # ---------------- CORS (split deployments only) ----------------
    # When the frontend is served from a different origin (e.g. Vercel),
    # browsers block cross-origin XHR until the API answers preflights.
    # Added AFTER the protections middleware above so CORS wraps it
    # (middleware added later runs first) — preflight OPTIONS requests
    # are then answered directly and never hit the rate limiter.
    # Empty CLIPPER_ALLOWED_ORIGINS (single-service Render deploy) keeps
    # the app lean: no CORS headers at all, exactly like before.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,  # we use no cookies/auth — nothing to send
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Range", "Accept"],
            expose_headers=["Content-Range", "Accept-Ranges", "Content-Length", "Content-Disposition"],
            max_age=3600,
        )
        log.info("CORS enabled for origins: %s", settings.cors_origins)

    # ---------------- routes ----------------
    app.include_router(routes_meta.router, prefix=api_prefix)
    app.include_router(routes_styles.router, prefix=api_prefix)
    app.include_router(routes_jobs.router, prefix=api_prefix)
    app.include_router(routes_previews.router, prefix=api_prefix)
    app.include_router(routes_ai.router, prefix=api_prefix)
    app.include_router(routes_media.router, prefix=api_prefix)

    # ---------------- static frontend (mounted last: / → index.html) ----------------
    frontend_dir = settings.resolved_frontend_dir
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
    else:  # pragma: no cover
        log.warning("frontend directory %s not found — serving API only", frontend_dir)

    return app


app = create_app()
