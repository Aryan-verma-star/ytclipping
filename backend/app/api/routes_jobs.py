"""Job endpoints: create, status, result, clip stream/download, history."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.errors import AppError, GoneError, NotFoundError, ValidationAppError
from app.core.validation import (
    extract_youtube_id,
    format_timecode,
    parse_time,
    validate_time_window,
)
from app.db import repo
from app.db.models import JobStatus, PreviewStatus
from app.styles.base import get_style, validate_params

router = APIRouter(tags=["jobs"])

_STATUS_VALUES = [s.value for s in JobStatus]


class CreateJobRequest(BaseModel):
    url: str
    start_time: str | float
    end_time: str | float
    style_id: str = "original"
    style_params: dict[str, Any] | None = None
    preview_id: str | None = None  # reuse the file a preview already downloaded


class JobOut(BaseModel):
    id: str
    source_url: str
    video_id: str
    video_title: str | None
    start_seconds: float
    end_seconds: float
    start_timecode: str
    end_timecode: str
    duration_seconds: float
    style_id: str
    style_params: dict
    status: str
    error: str | None
    provider: str | None
    notes: str | None
    preview_id: str | None
    output_size_bytes: int | None
    output_duration_seconds: float | None
    clip_url: str | None
    download_url: str | None
    file_deleted_at: str | None
    created_at: str
    updated_at: str


class JobListOut(BaseModel):
    items: list[JobOut]
    total: int
    limit: int
    offset: int


def _job_out(job) -> JobOut:
    completed = job.status == JobStatus.COMPLETED and job.output_filename
    clip_url = f"/api/jobs/{job.id}/clip" if completed else None
    return JobOut(
        id=job.id,
        source_url=job.source_url,
        video_id=job.video_id,
        video_title=job.video_title,
        start_seconds=job.start_seconds,
        end_seconds=job.end_seconds,
        start_timecode=format_timecode(job.start_seconds),
        end_timecode=format_timecode(job.end_seconds),
        duration_seconds=round(job.end_seconds - job.start_seconds, 3),
        style_id=job.style_id,
        style_params=job.style_params or {},
        status=job.status.value if hasattr(job.status, "value") else str(job.status),
        error=job.error,
        provider=job.provider,
        notes=job.notes,
        preview_id=job.preview_id,
        output_size_bytes=job.output_size_bytes,
        output_duration_seconds=job.output_duration_seconds,
        clip_url=clip_url,
        download_url=(f"{clip_url}?download=1" if clip_url else None),
        file_deleted_at=job.file_deleted_at.isoformat() if job.file_deleted_at else None,
        created_at=job.created_at.isoformat() if job.created_at else None,
        updated_at=job.updated_at.isoformat() if job.updated_at else None,
    )


def _validate_creation(payload: CreateJobRequest, settings, db):
    """Full request validation; returns every field needed to insert the job."""
    canonical_url, video_id = extract_youtube_id(payload.url)
    start = parse_time(payload.start_time, field="start_time")
    end = parse_time(payload.end_time, field="end_time")
    validate_time_window(
        start,
        end,
        max_clip_seconds=settings.max_clip_seconds,
        max_source_seconds=settings.max_source_seconds,
    )

    style = get_style(payload.style_id)
    if style is None:
        raise ValidationAppError(
            f"Unknown clip style {payload.style_id!r}.",
            field="style_id",
        )
    style_params = validate_params(style, payload.style_params or {})

    preview_ok = False
    if payload.preview_id:
        with db.session() as session:
            preview = repo.get_preview(session, payload.preview_id)
        if preview is None:
            raise ValidationAppError(
                "preview_id does not refer to a known preview — load the "
                "video again or omit preview_id.",
                field="preview_id",
            )
        if preview.status == PreviewStatus.EXPIRED:
            raise ValidationAppError(
                "This preview has passed the retention window — load the "
                "video again and re-select the range.",
                field="preview_id",
            )
        if preview.status == PreviewStatus.FAILED:
            raise ValidationAppError(
                "The preview for this video failed — load the video again "
                "or omit preview_id to download from scratch.",
                field="preview_id",
            )
        if preview.source_url != canonical_url:
            raise ValidationAppError(
                "preview_id belongs to a different URL than the one being clipped.",
                field="preview_id",
            )
        # In-flight previews (resolving/streaming/downloading/processing) are
        # fine: the job queues now and runs the moment the cached file lands.
        preview_ok = True

    return canonical_url, video_id, start, end, style_params, preview_ok


@router.post("/jobs", response_model=JobOut, status_code=202)
def create_job(payload: CreateJobRequest, request: Request) -> JobOut:
    settings = request.app.state.settings
    db = request.app.state.db

    canonical_url, video_id, start, end, style_params, preview_ok = _validate_creation(
        payload, settings, db
    )

    with db.session() as session:
        job = repo.create_job(
            session,
            id=uuid.uuid4().hex,
            source_url=canonical_url,
            video_id=video_id,
            start_seconds=start,
            end_seconds=end,
            style_id=payload.style_id,
            style_params=style_params,
            preview_id=(payload.preview_id if preview_ok else None),
            status=JobStatus.QUEUED,
        )
        return _job_out(job)


@router.get("/jobs", response_model=JobListOut)
def list_jobs(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, description="Filter by status"),
) -> JobListOut:
    db = request.app.state.db
    status_enum = None
    if status is not None:
        if status not in _STATUS_VALUES:
            raise ValidationAppError(
                f"Unknown status filter {status!r}. Valid values: {', '.join(_STATUS_VALUES)}.",
                field="status",
            )
        status_enum = JobStatus(status)
    with db.session() as session:
        items, total = repo.list_jobs(session, limit=limit, offset=offset, status=status_enum)
        return JobListOut(
            items=[_job_out(j) for j in items],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, request: Request) -> JobOut:
    db = request.app.state.db
    with db.session() as session:
        job = repo.get_job(session, job_id)
        if job is None:
            raise NotFoundError(f"Job {job_id} not found.")
        return _job_out(job)


@router.get("/jobs/{job_id}/clip")
def download_clip(job_id: str, request: Request, download: bool = False):
    """Stream (default) or download (download=1) the finished clip.

    Range requests are handled by the file response for video seeking.
    """
    settings = request.app.state.settings
    db = request.app.state.db

    with db.session() as session:
        job = repo.get_job(session, job_id)
        if job is None:
            raise NotFoundError(f"Job {job_id} not found.")
        if job.status != JobStatus.COMPLETED or not job.output_filename:
            raise AppError(
                f"Job {job_id} is not completed yet (status: {job.status.value}).",
                code="clip_not_ready",
                status_code=409,
            )
        if job.file_deleted_at is not None:
            raise GoneError(
                "This clip file has passed the retention window and was deleted. "
                "The history record is kept — resubmit the same range to clip it again."
            )
        output_filename = job.output_filename

    path = settings.clips_dir / output_filename
    if not path.is_file():
        raise GoneError(
            "The clip file is no longer on disk (service restarted or file was "
            "swept). Resubmit the same range to clip it again."
        )

    title = (job.video_title or job.video_id or "clip").replace('"', "'")
    safe_title = "".join(c if c.isalnum() or c in " ._-" else "_" for c in title)[:80]
    if download:
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=f"{safe_title} [{format_timecode(job.start_seconds)}-{format_timecode(job.end_seconds)}].mp4",
        )
    return FileResponse(path, media_type="video/mp4")


@router.delete("/jobs/{job_id}", response_model=JobOut)
def delete_job(job_id: str, request: Request) -> JobOut:
    """Delete a job's clip file immediately (metadata record is kept)."""
    settings = request.app.state.settings
    db = request.app.state.db
    from datetime import datetime, timezone

    with db.session() as session:
        job = repo.get_job(session, job_id)
        if job is None:
            raise NotFoundError(f"Job {job_id} not found.")
        if job.output_filename:
            path = settings.clips_dir / job.output_filename
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        job.file_deleted_at = job.file_deleted_at or datetime.now(timezone.utc)
        session.commit()
        session.refresh(job)
        return _job_out(job)
