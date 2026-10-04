"""Preview pipeline — powers the timeline UI.

Instant-load design (the "paste a link and it just plays" experience):

1. ``resolve``   — a provider cheaply resolves a direct, seekable media URL
   plus duration/title (cobalt tunnel URL, yt-dlp muxed format, …). The
   preview immediately becomes STREAMING: the browser plays the video through
   the backend's ``/stream`` proxy and the timeline renders, while…
2. ``download``  — …the full-quality file downloads in the BACKGROUND with a
   live progress fraction. Clip jobs created in the meantime simply wait for
   this cache (the job worker skips them until the preview is READY).
3. ``process``   — once the file lands, playback switches to the local file
   (same URL) and the filmstrip thumbnails are generated.
4. ``ready``     — thumbs served, clips cut from the cached file.

Providers without a resolvable stream (or the synthetic sample provider)
skip straight to DOWNLOADING; the timeline still appears instantly when the
duration is known (sample), or after the download otherwise (legacy
behavior).

Lifecycle: pending → resolving → streaming | downloading → processing →
ready | failed, plus expired after the retention window (files deleted, row
kept for a clean 410).
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings
from app.core.errors import AppError, GoneError, NotFoundError
from app.core.ffmpeg import FFmpegError, ffprobe_video_info, run_ffmpeg
from app.core.validation import format_timecode
from app.db.base import Database
from app.db import repo
from app.db.models import PREVIEW_ACTIVE_STATUSES, Preview, PreviewStatus
from app.downloader.base import DownloaderProvider, ProgressCB, VideoSource
from app.services.orchestrator import _download_with_chain, _resolve_stream_with_chain

log = logging.getLogger("clipper.preview")

_MAX_ERROR_CHARS = 1000
_THUMB_NAME_RE = re.compile(r"^\d{3}\.jpg$")
_PROGRESS_WRITE_INTERVAL = 0.5  # seconds between progress commits


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fail(db: Database, preview_id: str, message: str) -> None:
    with db.session() as session:
        preview = repo.get_preview(session, preview_id)
        if preview is None:
            return
        preview.status = PreviewStatus.FAILED
        preview.error = message[:_MAX_ERROR_CHARS]
        preview.progress = None
        preview.updated_at = _now()
        session.commit()
        # queued clip jobs waiting on this preview would never run — fail them
        # with the underlying reason instead of leaving them stuck.
        repo.fail_jobs_waiting_on_preview(
            session,
            preview_id,
            "The background source download for this clip failed: " + message[:800],
        )
    log.error("preview %s failed: %s", preview_id, message)


def delete_preview_files(preview: Preview) -> None:
    if preview.file_path:
        try:
            Path(preview.file_path).unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            log.warning("could not delete preview file %s", preview.file_path)
    if preview.thumb_dir:
        shutil.rmtree(preview.thumb_dir, ignore_errors=True)


def generate_thumbnails(
    source: Path,
    thumb_dir: Path,
    *,
    duration: float,
    count: int,
    height: int,
) -> list[str]:
    """`count` JPEG tiles evenly spaced across the video; returns filenames.

    Per-tile seeks (``-ss`` before ``-i``) keep this cheap even on very long
    sources — each invocation decodes a single frame, not the whole file.
    Individual tile failures are tolerated as long as most succeed.
    """
    thumb_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for i in range(count):
        # sample the middle of each tile's time span
        t = duration * (i + 0.5) / count if count > 0 else 0.0
        t = max(0.0, min(t, max(0.0, duration - 0.05)))
        name = f"{i:03d}.jpg"
        out = thumb_dir / name
        try:
            run_ffmpeg(
                [
                    "-ss",
                    f"{t:.3f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale=-2:{height}",
                    "-q:v",
                    "5",
                    str(out),
                ],
                timeout=60,
            )
        except FFmpegError:
            log.warning("thumbnail %d failed for %s", i, source)
            continue
        if out.exists() and out.stat().st_size > 0:
            names.append(name)
        else:
            try:
                out.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass
    if len(names) < max(3, count // 3):
        raise RuntimeError(
            f"thumbnail generation mostly failed ({len(names)}/{count} tiles) — "
            "the downloaded file may not be decodable video."
        )
    return names


def _make_progress_reporter(db: Database, preview_id: str) -> ProgressCB:
    """Persist the background-download fraction, throttled to ~2 writes/s."""

    def report(fraction: float | None) -> None:
        try:
            with db.session() as session:
                preview = repo.get_preview(session, preview_id)
                if preview is None or preview.status not in (
                    PreviewStatus.STREAMING,
                    PreviewStatus.DOWNLOADING,
                ):
                    return  # finished or failed meanwhile — stop updating
                preview.progress = None if fraction is None else max(0.0, min(float(fraction), 1.0))
                preview.updated_at = _now()
                session.commit()
        except Exception:  # pragma: no cover - progress reporting is best-effort
            log.debug("progress commit failed for %s", preview_id, exc_info=True)

    return report


def process_preview(
    preview_id: str,
    db: Database,
    settings: Settings,
    providers: list[DownloaderProvider],
) -> None:
    """Process one preview synchronously. Safe to call from tests or the worker."""
    with db.session() as session:
        preview = repo.get_preview(session, preview_id)
        if preview is None:
            log.warning("process_preview: %s not found", preview_id)
            return
        if preview.status != PreviewStatus.PENDING:
            log.info("process_preview: %s is %s, skipping", preview_id, preview.status.value)
            return
        # A usable preview for the same URL may have appeared while this one
        # waited in the queue (double paste) — stand aside instead of
        # downloading the same video twice.
        url = preview.source_url
        if repo.latest_usable_preview_for_url(session, url, _now()) not in (None, preview):
            preview.status = PreviewStatus.EXPIRED
            preview.error = "Superseded by another preview of the same URL."
            preview.updated_at = _now()
            session.commit()
            log.info("preview %s superseded by a newer one for the same URL", preview_id)
            return
        preview.status = PreviewStatus.RESOLVING
        preview.updated_at = _now()
        session.commit()

    work_dir = settings.previews_dir / preview_id
    downloaded: Path | None = None
    moved = False

    def fail(message: str) -> None:
        # full cleanup: drop the tmp download (if still there) and the work dir
        if downloaded is not None and not moved:
            try:
                downloaded.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                log.warning("could not remove tmp download %s", downloaded)
        shutil.rmtree(work_dir, ignore_errors=True)
        _fail(db, preview_id, message)

    try:
        work_dir.mkdir(parents=True, exist_ok=True)

        # ---- phase 1: resolve an instant-playback stream (fast, metadata-only)
        target = _resolve_stream_with_chain(providers, url, 0.0, settings.max_source_seconds)
        if target is not None:
            with db.session() as session:
                preview = repo.get_preview(session, preview_id)
                if preview is None or preview.status not in (
                    PreviewStatus.RESOLVING,
                    PreviewStatus.PENDING,
                ):
                    return  # state changed underneath us (expiry sweep, restart…)
                if target.duration and target.duration > 0:
                    preview.duration = float(target.duration)
                if target.title:
                    preview.title = target.title
                if target.playable:
                    preview.stream_url = target.url
                    preview.stream_headers = dict(target.headers or {})
                    preview.status = PreviewStatus.STREAMING
                else:
                    preview.status = PreviewStatus.DOWNLOADING
                preview.stream_provider = target.provider
                preview.progress = 0.0
                preview.updated_at = _now()
                session.commit()
            log.info(
                "preview %s resolved via %s: playable=%s duration=%s",
                preview_id,
                target.provider,
                target.playable,
                target.duration,
            )
        else:
            with db.session() as session:
                preview = repo.get_preview(session, preview_id)
                if preview is None or preview.status not in (
                    PreviewStatus.RESOLVING,
                    PreviewStatus.PENDING,
                ):
                    return
                preview.status = PreviewStatus.DOWNLOADING
                preview.updated_at = _now()
                session.commit()

        # ---- phase 2: background full download (the preview needs the WHOLE
        # video — the timeline spans its full length).
        report = _make_progress_reporter(db, preview_id)
        source = _download_with_chain(
            providers,
            url,
            0.0,
            settings.max_source_seconds,
            progress=report,
            reuse=target,
        )
        downloaded = source.path

        duration, width, height = ffprobe_video_info(source.path)
        if duration is None or duration <= 0:
            fail(
                "Could not determine the video duration — the downloaded file "
                "may be corrupt or not a video."
            )
            return
        if duration > settings.max_source_seconds + 1.0:
            fail(
                f"Source video is {format_timecode(duration)} long, longer than the "
                f"configured maximum of {format_timecode(settings.max_source_seconds)}."
            )
            return

        # Move the source file into the preview's own directory so it outlives
        # the tmp cleanup conventions and is owned by this preview record.
        # file_path + duration land BEFORE thumbnails: playback can switch
        # from the proxy to the local file while thumbs still generate.
        final_path = work_dir / f"source{source.path.suffix or '.mp4'}"
        if source.path.resolve() != final_path.resolve():
            shutil.move(str(source.path), str(final_path))
        else:  # pragma: no cover
            final_path = source.path
        moved = True

        with db.session() as session:
            preview = repo.get_preview(session, preview_id)
            preview.status = PreviewStatus.PROCESSING
            preview.provider = source.provider
            preview.title = source.title or preview.title
            preview.file_path = str(final_path)
            preview.duration = duration
            preview.width = width
            preview.height = height
            preview.progress = 1.0
            preview.updated_at = _now()
            session.commit()

        # ---- phase 3: filmstrip thumbnails
        thumb_dir = work_dir / "thumbs"
        names = generate_thumbnails(
            final_path,
            thumb_dir,
            duration=duration,
            count=settings.preview_thumb_count,
            height=settings.preview_thumb_height,
        )

        with db.session() as session:
            preview = repo.get_preview(session, preview_id)
            preview.status = PreviewStatus.READY
            preview.error = None
            preview.thumb_dir = str(thumb_dir)
            preview.thumb_count = len(names)
            preview.expires_at = _now() + timedelta(hours=settings.preview_retention_hours)
            preview.updated_at = _now()
            session.commit()
        log.info(
            "preview %s ready: %.1fs, %d thumbs, via %s",
            preview_id,
            duration,
            len(names),
            source.provider,
        )
    except Exception as exc:
        log.exception("preview %s errored during processing", preview_id)
        message = str(exc) or repr(exc)
        fail(message[:_MAX_ERROR_CHARS])


def resolve_preview_source(db: Database, preview_id: str) -> VideoSource | None:
    """Build a VideoSource from a READY preview's file, or None when unusable.

    Used by the job orchestrator to skip a second download. The file remains
    owned by the preview (retention sweeper deletes it) — callers must NOT
    unlink it.
    """
    with db.session() as session:
        preview = repo.get_preview(session, preview_id)
        if preview is None or preview.status != PreviewStatus.READY:
            return None
        file_path = preview.file_path
        data = {
            "segment_start": 0.0,
            "title": preview.title,
            "duration": preview.duration,
            "provider": preview.provider,
            "metadata": {"reused_preview": preview.id},
        }
    if not file_path or not Path(file_path).is_file():
        return None
    return VideoSource(path=Path(file_path), **data)


class PreviewWorker(threading.Thread):
    """Processes pending previews, one at a time (same policy as job worker)."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        providers: list[DownloaderProvider],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="clipper-preview-worker")
        self.db = db
        self.settings = settings
        self.providers = providers
        self.stop_event = stop_event
        # supervision hooks: what this worker is inside right now (None = idle)
        self.current_id: str | None = None
        self.current_since: float | None = None

    def run(self) -> None:
        log.info("preview worker started")
        while not self.stop_event.is_set():
            try:
                with self.db.session() as session:
                    preview_id = repo.oldest_pending_preview_id(session)
                if preview_id is None:
                    self.stop_event.wait(self.settings.worker_poll_interval_seconds)
                    continue
                self.current_id = preview_id
                self.current_since = time.monotonic()
                try:
                    process_preview(preview_id, self.db, self.settings, self.providers)
                finally:
                    self.current_id = None
                    self.current_since = None
            except Exception:  # pragma: no cover - the loop must never die
                log.exception("preview worker loop iteration failed")
                self.stop_event.wait(self.settings.worker_poll_interval_seconds)
        log.info("preview worker stopped")

    def stop(self) -> None:
        self.stop_event.set()


def fail_stuck_preview(db: Database, preview_id: str, message: str | None = None) -> None:
    """Supervisor hook: a preview stuck in one worker for too long becomes failed.

    The worker thread itself is replaced by the supervisor — this only fixes
    the row (and fails jobs waiting on the preview) so the UI stops waiting
    and shows an actionable message.
    """
    text = message or (
        "Preparing this video took unusually long (the server was busy or the "
        "source too heavy) and was stopped. Please load it again — the retry "
        "starts fresh, and uploads always work."
    )
    _fail(db, preview_id, text)


def recover_stale_previews(db: Database) -> int:
    """Startup recovery: previews interrupted by a restart become failed.

    Uses repo.fail_stale_previews for the bulk update, then cascades the
    failure to queued jobs that were waiting on each preview's file.
    """
    from sqlalchemy import select

    with db.session() as session:
        stale = list(
            session.scalars(select(Preview).where(Preview.status.in_(PREVIEW_ACTIVE_STATUSES)))
        )
        stale_ids = [p.id for p in stale]
        message = (
            "Service restarted while this preview was being prepared. "
            "Load the video again."
        )
        repo.fail_stale_previews(session, message)
        for preview_id in stale_ids:
            repo.fail_jobs_waiting_on_preview(
                session,
                preview_id,
                "The background source download was interrupted by a service "
                "restart. Please create the clip again.",
            )
    if stale_ids:
        log.warning("recovered %d stale preview(s) as failed", len(stale_ids))
    return len(stale_ids)


def reconcile_ready_previews_with_disk(db: Database, previews_dir: Path) -> int:
    """Boot reconciliation for ephemeral filesystems (Render free tier).

    With a durable DATABASE_URL the preview ROWS survive restarts, but the
    downloaded source FILES live on the instance disk and do not. A READY
    row whose file is gone would otherwise serve a broken editor (404 media)
    and fail every clip job that reuses it. Mark such previews failed with a
    clear message and cascade the failure to queued jobs waiting on them —
    the user simply loads the video again.
    """
    from sqlalchemy import select

    reconciled = 0
    with db.session() as session:
        ready = list(
            session.scalars(select(Preview).where(Preview.status == PreviewStatus.READY))
        )
        for preview in ready:
            work_dir = previews_dir / preview.id
            has_file = any(work_dir.glob("source.*"))
            if has_file:
                continue
            preview.status = PreviewStatus.FAILED
            preview.error = (
                "The cached source file was lost in a service restart "
                "(free-tier disk is ephemeral). Load the video again."
            )
            preview.updated_at = _now()
            repo.fail_jobs_waiting_on_preview(
                session,
                preview.id,
                "The background source file was lost in a service restart. "
                "Please load the video and create the clip again.",
            )
            reconciled += 1
        if reconciled:
            session.commit()
    if reconciled:
        log.warning(
            "reconciled %d ready preview(s) whose files were lost with the disk",
            reconciled,
        )
    return reconciled


# --------------- route helpers (shared response shaping) ------------------


def require_ready_preview(db: Database, preview_id: str) -> Preview:
    """404 unknown ids, 410 expired/failed-with-files, 409 still processing."""
    with db.session() as session:
        preview = repo.get_preview(session, preview_id)
        if preview is None:
            raise NotFoundError(f"Preview {preview_id} not found.")
        if preview.status == PreviewStatus.EXPIRED:
            raise GoneError(
                "This preview has passed the retention window and its files "
                "were deleted. Load the video again."
            )
        if preview.status == PreviewStatus.FAILED:
            raise AppError(
                preview.error or "This preview failed.",
                code="preview_failed",
                status_code=409,
            )
        if preview.status != PreviewStatus.READY:
            raise AppError(
                f"Preview is still being prepared (status: {preview.status.value}).",
                code="preview_not_ready",
                status_code=409,
            )
        return preview


def playable_preview_or_error(db: Database, preview_id: str) -> Preview:
    """Guard for the /stream endpoint — playable means file OR proxy target.

    Differs from require_ready_preview: STREAMING (proxy alive) and
    PROCESSING (local file landed, thumbs still generating) are both fine.
    """
    with db.session() as session:
        preview = repo.get_preview(session, preview_id)
        if preview is None:
            raise NotFoundError(f"Preview {preview_id} not found.")
        if preview.status == PreviewStatus.EXPIRED:
            raise GoneError(
                "This preview has passed the retention window and its files "
                "were deleted. Load the video again."
            )
        if preview.status == PreviewStatus.FAILED:
            raise AppError(
                preview.error or "This preview failed.",
                code="preview_failed",
                status_code=409,
            )
        playable = (preview.file_path and Path(preview.file_path).is_file()) or bool(preview.stream_url)
        if not playable:
            raise AppError(
                "The preview video is not ready for playback yet — hold on a "
                "moment while the source is being prepared.",
                code="preview_not_playable",
                status_code=409,
            )
        return preview


def thumb_path(preview: Preview, name: str) -> Path:
    """Validated path of a thumbnail file (guards path traversal)."""
    if not _THUMB_NAME_RE.match(name) or not preview.thumb_dir:
        raise NotFoundError("No such thumbnail.")
    path = Path(preview.thumb_dir) / name
    if not path.is_file():
        raise NotFoundError("No such thumbnail (index out of range or missing).")
    return path
