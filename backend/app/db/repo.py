"""Thin repository functions over the Job and Preview models."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    ACTIVE_STATUSES,
    PREVIEW_ACTIVE_STATUSES,
    Job,
    JobStatus,
    Preview,
    PreviewStatus,
)


def create_job(session: Session, **fields) -> Job:
    job = Job(**fields)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def get_job(session: Session, job_id: str) -> Job | None:
    return session.get(Job, job_id)


def list_jobs(
    session: Session,
    *,
    limit: int = 20,
    offset: int = 0,
    status: JobStatus | None = None,
) -> tuple[list[Job], int]:
    stmt = select(Job).order_by(Job.created_at.desc(), Job.id.desc())
    count_stmt = select(func.count()).select_from(Job)
    if status is not None:
        stmt = stmt.where(Job.status == status)
        count_stmt = count_stmt.where(Job.status == status)
    total = session.scalar(count_stmt) or 0
    items = list(session.scalars(stmt.limit(limit).offset(offset)))
    return items, total


def oldest_queued_job_id(session: Session) -> str | None:
    stmt = (
        select(Job.id)
        .where(Job.status == JobStatus.QUEUED)
        .order_by(Job.created_at.asc(), Job.id.asc())
        .limit(1)
    )
    return session.scalar(stmt)


def oldest_runnable_job_id(session: Session) -> str | None:
    """Oldest queued job the worker can process RIGHT NOW.

    Jobs that reference a preview which is still downloading/streaming are
    skipped — they become runnable the moment the preview's file is READY.
    This is what lets the user hit "Create clip" while the background
    download is still running.
    """
    ready_preview = (
        select(Preview.id)
        .where(Preview.id == Job.preview_id)
        .where(Preview.status == PreviewStatus.READY)
        .exists()
    )
    stmt = (
        select(Job.id)
        .where(Job.status == JobStatus.QUEUED)
        .where(or_(Job.preview_id.is_(None), ready_preview))
        .order_by(Job.created_at.asc(), Job.id.asc())
        .limit(1)
    )
    return session.scalar(stmt)


def fail_jobs_waiting_on_preview(session: Session, preview_id: str, message: str) -> int:
    """Fail queued jobs whose preview will never become READY.

    Called whenever a preview fails, expires, or is recovered as stale —
    otherwise those jobs would wait in the queue forever.
    """
    stmt = select(Job).where(
        Job.status == JobStatus.QUEUED,
        Job.preview_id == preview_id,
    )
    waiting = list(session.scalars(stmt))
    for job in waiting:
        job.status = JobStatus.FAILED
        job.error = message[:1000]
        job.updated_at = datetime.now(timezone.utc)
    if waiting:
        session.commit()
    return len(waiting)


def fail_stale_jobs(session: Session, message: str) -> int:
    """Mark jobs stuck in downloading/clipping as failed (startup recovery)."""
    stmt = select(Job).where(Job.status.in_(ACTIVE_STATUSES))
    stale = list(session.scalars(stmt))
    for job in stale:
        job.status = JobStatus.FAILED
        job.error = message
        job.updated_at = datetime.now(timezone.utc)
    if stale:
        session.commit()
    return len(stale)


def expired_clip_jobs(session: Session, cutoff: datetime) -> list[Job]:
    """Completed jobs whose files are past retention but not yet deleted."""
    stmt = select(Job).where(
        Job.status == JobStatus.COMPLETED,
        Job.file_deleted_at.is_(None),
        Job.created_at < cutoff,
    )
    return list(session.scalars(stmt))


# ------------------------------- previews ---------------------------------


def create_preview(session: Session, **fields) -> Preview:
    preview = Preview(**fields)
    session.add(preview)
    session.commit()
    session.refresh(preview)
    return preview


def get_preview(session: Session, preview_id: str) -> Preview | None:
    return session.get(Preview, preview_id)


def latest_usable_preview_for_url(session: Session, source_url: str, now: datetime) -> Preview | None:
    """A preview for exactly this canonical URL that a new request can use.

    Dedupes BOTH ready previews (file already cached) and in-flight ones
    (still resolving/downloading) — pasting the same URL twice must not start
    a second full download. Terminal states (failed/expired) never dedupe.
    """
    stmt = (
        select(Preview)
        .where(
            Preview.source_url == source_url,
            Preview.status.in_(PREVIEW_ACTIVE_STATUSES + (PreviewStatus.READY,)),
            Preview.expires_at.is_(None) | (Preview.expires_at > now),
        )
        .order_by(Preview.created_at.desc(), Preview.id.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def latest_ready_preview_for_url(session: Session, source_url: str, now: datetime) -> Preview | None:
    """A READY, non-expired preview for exactly this canonical URL (dedupe)."""
    stmt = (
        select(Preview)
        .where(
            Preview.source_url == source_url,
            Preview.status == PreviewStatus.READY,
            Preview.expires_at.is_(None) | (Preview.expires_at > now),
        )
        .order_by(Preview.created_at.desc(), Preview.id.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def oldest_pending_preview_id(session: Session) -> str | None:
    stmt = (
        select(Preview.id)
        .where(Preview.status == PreviewStatus.PENDING)
        .order_by(Preview.created_at.asc(), Preview.id.asc())
        .limit(1)
    )
    return session.scalar(stmt)


def fail_stale_previews(session: Session, message: str, *, cutoff: datetime | None = None) -> int:
    """Mark previews stuck in an active status as failed.

    At startup every active preview is failed (a restart lost the file state);
    the sweeper additionally fails previews that have been processing longer
    than the allowed window.
    """
    stmt = select(Preview).where(Preview.status.in_(PREVIEW_ACTIVE_STATUSES))
    if cutoff is not None:
        stmt = stmt.where(Preview.created_at < cutoff)
    stale = list(session.scalars(stmt))
    for preview in stale:
        preview.status = PreviewStatus.FAILED
        preview.error = message
        preview.updated_at = datetime.now(timezone.utc)
    if stale:
        session.commit()
    return len(stale)


def expired_ready_previews(session: Session, now: datetime) -> list[Preview]:
    """READY previews past their expires_at (files should be deleted)."""
    stmt = select(Preview).where(
        Preview.status == PreviewStatus.READY,
        Preview.expires_at.is_not(None),
        Preview.expires_at <= now,
    )
    return list(session.scalars(stmt))
