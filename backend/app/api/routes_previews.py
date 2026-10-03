"""Preview endpoints: create (async), poll status, filmstrip thumbs, source video.

The instant-load contract:
- ``POST /api/previews`` — 202 immediately; dedupes both READY and in-flight
  previews for the same canonical URL.
- ``GET  /api/previews/{id}`` — the poll payload. As soon as a provider
  resolves the video, ``duration`` (timeline!) and ``video_url`` (playback!)
  appear even while ``status`` is still ``streaming``/``downloading`` and
  ``progress`` (0..1) reports the background cache download.
- ``GET  /api/previews/{id}/stream`` — ONE playback URL for the preview's
  whole life: proxies the provider's direct media URL (Range requests are
  forwarded, so seeking works) until the local file lands, then serves the
  file. The player never has to switch sources.
- ``GET  /api/previews/{id}/video`` — legacy alias for the local file
  (READY previews only).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.core.errors import GoneError, NotFoundError
from app.core.validation import extract_youtube_id
from app.db import repo
from app.db.models import PreviewStatus
from app.services.preview import playable_preview_or_error, require_ready_preview, thumb_path

router = APIRouter(tags=["previews"])

# Headers worth relaying from the upstream media response to the browser.
_RELAY_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "etag",
    "last-modified",
)

_PROXY_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


class CreatePreviewRequest(BaseModel):
    url: str


class PreviewOut(BaseModel):
    id: str
    url: str
    video_id: str
    status: str
    error: str | None
    provider: str | None
    stream_provider: str | None
    title: str | None
    duration: float | None
    width: int | None
    height: int | None
    thumb_count: int
    thumbs: list[str]
    video_url: str | None
    progress: float | None
    expires_at: str | None
    created_at: str
    updated_at: str


def _is_playable(preview) -> bool:
    """Playback is possible via /stream: local file on disk OR a live proxy target."""
    if preview.file_path and Path(preview.file_path).is_file():
        return True
    return bool(preview.stream_url)


def _preview_out(preview, *, with_files: bool) -> PreviewOut:
    ready = preview.status == PreviewStatus.READY
    thumbs: list[str] = []
    if ready and with_files and preview.thumb_dir:
        thumbs_dir = Path(preview.thumb_dir)
        if thumbs_dir.is_dir():
            thumbs = sorted(
                f"/api/previews/{preview.id}/thumbs/{p.name}" for p in thumbs_dir.iterdir()
            )
    progress = preview.progress
    if ready:
        progress = 1.0
    return PreviewOut(
        id=preview.id,
        url=preview.source_url,
        video_id=preview.video_id,
        status=preview.status.value if hasattr(preview.status, "value") else str(preview.status),
        error=preview.error,
        provider=preview.provider,
        stream_provider=preview.stream_provider,
        title=preview.title,
        duration=preview.duration,
        width=preview.width,
        height=preview.height,
        thumb_count=preview.thumb_count,
        thumbs=thumbs,
        video_url=(
            f"/api/previews/{preview.id}/stream"
            if (ready or _is_playable(preview)) and with_files
            else None
        ),
        progress=progress,
        expires_at=preview.expires_at.isoformat() if preview.expires_at else None,
        created_at=preview.created_at.isoformat() if preview.created_at else None,
        updated_at=preview.updated_at.isoformat() if preview.updated_at else None,
    )


@router.post("/previews", response_model=PreviewOut, status_code=202)
def create_preview(payload: CreatePreviewRequest, request: Request) -> PreviewOut:
    """Queue a source-video preview (instant stream + background download).

    The frontend polls GET /api/previews/{id} and renders the player +
    timeline as soon as ``video_url``/``duration`` appear. Asking again for
    the same URL returns the existing usable preview (ready or in-flight)
    instead of starting a second download.
    """
    db = request.app.state.db
    canonical_url, video_id = extract_youtube_id(payload.url)

    with db.session() as session:
        existing = repo.latest_usable_preview_for_url(
            session, canonical_url, datetime.now(timezone.utc)
        )
        if existing is not None:
            # In-flight previews are returned as-is (the client keeps polling);
            # READY ones must still have their file on disk to be reusable.
            file_ok = existing.file_path and Path(existing.file_path).is_file()
            if existing.status != PreviewStatus.READY or file_ok:
                return _preview_out(existing, with_files=True)

        preview = repo.create_preview(
            session,
            id=uuid.uuid4().hex,
            source_url=canonical_url,
            video_id=video_id,
            status=PreviewStatus.PENDING,
        )
        return _preview_out(preview, with_files=False)


@router.get("/previews/{preview_id}", response_model=PreviewOut)
def get_preview(preview_id: str, request: Request) -> PreviewOut:
    db = request.app.state.db
    with db.session() as session:
        preview = repo.get_preview(session, preview_id)
        if preview is None:
            raise NotFoundError(f"Preview {preview_id} not found.")
        return _preview_out(preview, with_files=True)


@router.get("/previews/{preview_id}/thumbs/{name}")
def preview_thumb(preview_id: str, name: str, request: Request) -> FileResponse:
    db = request.app.state.db
    preview = require_ready_preview(db, preview_id)
    return FileResponse(thumb_path(preview, name), media_type="image/jpeg")


@router.get("/previews/{preview_id}/video")
def preview_video(preview_id: str, request: Request) -> FileResponse:
    """Legacy alias: stream the downloaded source file (READY previews only).

    Range requests are handled by the file response for video seeking.
    """
    db = request.app.state.db
    preview = require_ready_preview(db, preview_id)
    path = Path(preview.file_path or "")
    if not path.is_file():
        raise GoneError(
            "The preview video file is no longer on disk (service restarted or "
            "retention swept it). Load the video again."
        )
    return FileResponse(path, media_type="video/mp4")


@router.get("/previews/{preview_id}/stream")
async def preview_stream(preview_id: str, request: Request):
    """The preview's single playback URL for its entire lifetime.

    - Local file on disk (processing/ready) → served directly; FileResponse
      handles Range requests (video seeking).
    - Still streaming from the provider → the stored, provider-resolved URL
      is proxied with the browser's Range header forwarded so the <video>
      element can seek before the download finishes.
    """
    db = request.app.state.db
    preview = playable_preview_or_error(db, preview_id)

    file_path = preview.file_path
    stream_url = preview.stream_url
    stream_headers = dict(preview.stream_headers or {})

    if file_path and Path(file_path).is_file():
        return FileResponse(file_path, media_type="video/mp4")

    range_header = request.headers.get("range")
    headers = {k: v for k, v in stream_headers.items() if isinstance(k, str) and isinstance(v, str)}
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(follow_redirects=True, timeout=_PROXY_TIMEOUT)
    try:
        upstream = await client.send(
            client.build_request("GET", stream_url, headers=headers),
            stream=True,
        )
    except httpx.HTTPError:
        await client.aclose()
        raise GoneError(
            "The instant stream from the provider is no longer reachable. "
            "The cached copy is still being prepared — retry in a moment."
        )

    relayed = {
        k: v for k, v in upstream.headers.items() if k.lower() in _RELAY_HEADERS
    }

    async def _close() -> None:
        await upstream.aclose()
        await client.aclose()

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=relayed,
        background=BackgroundTask(_close),
    )
