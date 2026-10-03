"""Health and metadata endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.ffmpeg import binaries_available, ffmpeg_version

log = logging.getLogger("clipper.health")

router = APIRouter(tags=["meta"])


@router.get("/health")
def health(request: Request) -> JSONResponse:
    db = request.app.state.db
    settings = request.app.state.settings

    db_ok = True
    try:
        with db.session() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:
        log.exception("health: database check failed")
        db_ok = False
        db_error = str(exc)
    else:
        db_error = None

    ffmpeg_ok = binaries_available()

    # Worker liveness, reported per thread via the supervisor (the watchdog
    # heals dead/stuck threads within one audit interval — flags here are for
    # diagnosis, not for failing the deploy health check).
    sup = getattr(request.app.state, "supervisor", None)

    def _worker_state(name: str) -> str:
        if sup is None:
            return "disabled"
        alive = sup.is_alive(name)
        if alive is None:
            return "unmanaged"
        repl = sup.replacements(name)
        return ("running" if alive else "dead") + (f"(replaced {repl}x)" if repl else "")

    body = {
        "status": "ok" if (db_ok and ffmpeg_ok) else "degraded",
        "checks": {
            "database": "ok" if db_ok else "error",
            "ffmpeg": "ok" if ffmpeg_ok else "missing",
            "job_worker": _worker_state("job_worker"),
            "preview_worker": _worker_state("preview_worker"),
            "retention_sweeper": _worker_state("retention_sweeper"),
        },
        "provider_chain": [p.name for p in request.app.state.providers],
        "ffmpeg_version": ffmpeg_version(),
        "environment": settings.environment,
        "version": request.app.version,
    }
    if db_error:
        body["checks"]["database_error"] = db_error[:300]
    status = 200 if (db_ok and ffmpeg_ok) else 503
    return JSONResponse(body, status_code=status)


@router.get("/meta")
def meta(request: Request) -> dict:
    """Limits and configuration the UI should display."""
    settings = request.app.state.settings
    from app.core.validation import format_timecode

    return {
        "app_name": settings.app_name,
        "environment": settings.environment,
        "limits": {
            "max_clip_seconds": settings.max_clip_seconds,
            "max_clip_timecode": format_timecode(settings.max_clip_seconds),
            "max_source_seconds": settings.max_source_seconds,
            "max_source_timecode": format_timecode(settings.max_source_seconds),
            "max_video_height": settings.max_video_height,
        },
        "retention_hours": settings.clip_retention_hours,
        "rate_limits": {
            "requests_per_minute": settings.rate_limit_per_minute,
            "jobs_per_minute": settings.rate_limit_jobs_per_minute,
        },
        "providers": [p.name for p in request.app.state.providers],
        "ai": {
            "enabled": settings.ai_analyzer_enabled,
            "endpoint": "/api/ai/suggest",
            "note": "Phase 4 extension point — not implemented (by design).",
        },
        "notice": (
            "Downloading YouTube content through third-party services may violate "
            "YouTube's Terms of Service and may infringe copyright depending on the "
            "video and your use. You are responsible for having the rights to clip "
            "and use this content."
        ),
    }
