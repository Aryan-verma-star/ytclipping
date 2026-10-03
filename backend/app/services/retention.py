"""Retention sweeper (spec §7).

- Clip files older than CLIP_RETENTION_HOURS are deleted; the job record is
  kept forever and marked with file_deleted_at (the clip endpoint then
  returns HTTP 410 with a clear message).
- Preview files older than PREVIEW_RETENTION_HOURS are deleted and the row is
  marked expired (its endpoints return 410). Previews stuck in an active
  status longer than PREVIEW_MAX_PROCESSING_MINUTES are failed.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.db.base import Database
from app.db import repo
from app.db.models import PREVIEW_ACTIVE_STATUSES, Preview, PreviewStatus
from app.services.preview import delete_preview_files
from app.services.uploads import sweep_uploads
from sqlalchemy import select

log = logging.getLogger("clipper.retention")


def sweep_once(db: Database, settings: Settings) -> int:
    now = datetime.now(timezone.utc)
    removed = _sweep_clips(db, settings, now)
    removed += _sweep_previews(db, settings, now)
    removed += sweep_uploads(settings, now=now)
    return removed


def _sweep_clips(db: Database, settings: Settings, now: datetime) -> int:
    cutoff = now - timedelta(hours=settings.clip_retention_hours)
    removed = 0
    with db.session() as session:
        for job in repo.expired_clip_jobs(session, cutoff):
            if job.output_filename:
                path = settings.clips_dir / job.output_filename
                try:
                    path.unlink(missing_ok=True)
                except OSError:  # pragma: no cover
                    log.warning("could not delete clip file %s", path)
            job.file_deleted_at = datetime.now(timezone.utc)
            removed += 1
        session.commit()
    if removed:
        log.info("retention sweep removed %d clip file(s) (metadata kept)", removed)
    return removed


def _sweep_previews(db: Database, settings: Settings, now: datetime) -> int:
    removed = 0
    with db.session() as session:
        # 1) ready previews past their expiry: delete files, mark expired
        for preview in repo.expired_ready_previews(session, now):
            delete_preview_files(preview)
            preview.status = PreviewStatus.EXPIRED
            preview.updated_at = now
            removed += 1
            # queued jobs waiting on this preview's file can never run now
            repo.fail_jobs_waiting_on_preview(
                session,
                preview.id,
                "The cached source for this clip expired (retention window) "
                "before the clip could be created. Please try again.",
            )
        # 2) previews stuck processing beyond the hard wall: fail them
        stuck_cutoff = now - timedelta(minutes=settings.preview_max_processing_minutes)
        stuck = list(
            session.scalars(
                select(Preview).where(
                    Preview.status.in_(PREVIEW_ACTIVE_STATUSES),
                    Preview.created_at < stuck_cutoff,
                )
            )
        )
        timeout_message = (
            "Preview preparation timed out (the source download or thumbnail "
            "generation took too long). Try again, possibly with a shorter video."
        )
        for preview in stuck:
            preview.status = PreviewStatus.FAILED
            preview.error = timeout_message
            preview.updated_at = now
            removed += 1
            repo.fail_jobs_waiting_on_preview(
                session,
                preview.id,
                "The background source download timed out. Please try again.",
            )
        session.commit()
    if removed:
        log.info("retention sweep handled %d preview(s)", removed)
    return removed


class RetentionSweeper(threading.Thread):
    def __init__(self, db: Database, settings: Settings, stop_event: threading.Event) -> None:
        super().__init__(daemon=True, name="clipper-retention-sweeper")
        self.db = db
        self.settings = settings
        self.stop_event = stop_event

    def run(self) -> None:
        log.info(
            "retention sweeper started (clips=%sh, previews=%sh, uploads=%sh, interval=%ss)",
            self.settings.clip_retention_hours,
            self.settings.preview_retention_hours,
            self.settings.upload_retention_hours,
            self.settings.retention_sweep_interval_seconds,
        )
        while not self.stop_event.is_set():
            try:
                sweep_once(self.db, self.settings)
            except Exception:  # pragma: no cover - the loop must never die
                log.exception("retention sweep failed")
            self.stop_event.wait(self.settings.retention_sweep_interval_seconds)
