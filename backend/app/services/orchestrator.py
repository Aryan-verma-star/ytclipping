"""Job orchestrator — runs a single job through the pipeline.

Status flow: queued → downloading → clipping → completed | failed.

Failure philosophy (spec §5/§9): any provider or ffmpeg failure becomes a
`failed` job with a clear, user-readable message. The process never crashes,
temp files are always cleaned up, and every transition is persisted.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import Settings
from app.core.ffmpeg import ffprobe_duration
from app.core.validation import format_timecode
from app.db.base import Database
from app.db.models import JobStatus
from app.db import repo
from app.downloader.base import (
    DownloaderProvider,
    ProgressCB,
    ProviderError,
    StreamTarget,
    VideoSource,
)
from app.styles.base import get_style

log = logging.getLogger("clipper.orchestrator")

_MAX_ERROR_CHARS = 1000


def _resolve_stream_with_chain(
    providers: list[DownloaderProvider], url: str, start: float, end: float
) -> StreamTarget | None:
    """First provider that can cheaply resolve an instant-playback stream.

    Errors are swallowed and logged: resolve is a best-effort optimization,
    and the plain download path reports the real error when things are broken.
    """
    for provider in providers:
        try:
            target = provider.resolve_stream(url, start, end)
        except Exception:  # unexpected — must never break the pipeline
            log.exception("provider %s resolve_stream raised", provider.name)
            continue
        if target is not None and (target.playable or target.duration):
            log.info("instant stream resolved via %s (playable=%s)", provider.name, target.playable)
            return target
        if target is not None:
            log.debug("provider %s resolved a target with neither url nor duration", provider.name)
    return None


def _download_with_chain(
    providers: list[DownloaderProvider],
    url: str,
    start: float,
    end: float,
    progress: ProgressCB | None = None,
    reuse: StreamTarget | None = None,
) -> VideoSource:
    """Try each provider in order; collect every failure reason.

    ``progress`` is forwarded to providers that support live reporting, and
    ``reuse`` lets the provider that resolved the instant stream (when it
    supports it, e.g. cobalt) download the very same URL without a second
    resolution round-trip.
    """
    errors: list[str] = []
    for provider in providers:
        try:
            kwargs: dict = {}
            if (
                reuse is not None
                and reuse.provider == provider.name
                and getattr(provider, "supports_reuse", False)
            ):
                kwargs["reuse"] = reuse
            source = provider.get_video(url, start, end, progress=progress, **kwargs)
            if source is None:
                raise ProviderError("provider returned nothing", provider=provider.name)
            log.info("job source acquired via %s", provider.name)
            return source
        except ProviderError as exc:
            errors.append(f"[{exc.provider}] {exc.message}")
            log.warning("provider %s failed: %s", exc.provider, exc.message)
        except TypeError:
            # a provider with a legacy signature (no progress kwarg) — retry plain
            try:
                source = provider.get_video(url, start, end)
            except ProviderError as exc:
                errors.append(f"[{exc.provider}] {exc.message}")
                log.warning("provider %s failed: %s", exc.provider, exc.message)
                continue
            except Exception as exc:
                errors.append(f"[{provider.name}] unexpected error: {exc!r}")
                log.exception("provider %s raised unexpectedly", provider.name)
                continue
            if source is None:
                errors.append(f"[{provider.name}] provider returned nothing")
                continue
            log.info("job source acquired via %s (legacy signature)", provider.name)
            return source
        except Exception as exc:  # unexpected — still must not crash the worker
            errors.append(f"[{provider.name}] unexpected error: {exc!r}")
            log.exception("provider %s raised unexpectedly", provider.name)
    raise ProviderError(
        "All download providers failed — " + " | ".join(errors), provider="chain"
    )


def _fail(db: Database, job_id: str, message: str) -> None:
    with db.session() as session:
        job = repo.get_job(session, job_id)
        if job is None:
            return
        job.status = JobStatus.FAILED
        job.error = message[:_MAX_ERROR_CHARS]
        job.updated_at = _now()
        session.commit()
    log.error("job %s failed: %s", job_id, message)


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _resolve_uploaded_source(settings, upload_ref: str) -> VideoSource | None:
    """An upload:// source ref → VideoSource, or None when swept/expired."""
    from app.services.uploads import upload_as_source

    upload_id = upload_ref[len("upload://") :]
    return upload_as_source(settings, upload_id)


def process_job(
    job_id: str,
    db: Database,
    settings: Settings,
    providers: list[DownloaderProvider],
) -> None:
    """Process one job synchronously. Safe to call from tests or the worker."""
    with db.session() as session:
        job = repo.get_job(session, job_id)
        if job is None:
            log.warning("process_job: job %s not found", job_id)
            return
        if job.status != JobStatus.QUEUED:
            log.info("process_job: job %s is %s, skipping", job_id, job.status.value)
            return
        url = job.source_url
        start = float(job.start_seconds)
        end = float(job.end_seconds)
        style_id = job.style_id
        style_params = dict(job.style_params or {})
        preview_id = job.preview_id
        is_upload = url.startswith("upload://")
        # uploaded sources need no download — go straight to clipping
        job.status = JobStatus.CLIPPING if is_upload else JobStatus.DOWNLOADING
        job.updated_at = _now()
        session.commit()

    tmp_to_cleanup: list[Path] = []
    reused = None  # preview file reuse — upload/normal sources may set nothing
    try:
        if is_upload:
            source = _resolve_uploaded_source(settings, url)
            if source is None:
                _fail(
                    db,
                    job_id,
                    "The uploaded source file is no longer available (expired "
                    "or swept by retention). Upload the file and try again.",
                )
                return
            log.info("job %s source is upload %s", job_id, url)
        else:
            reused = None
            if preview_id:
                from app.services.preview import resolve_preview_source

                reused = resolve_preview_source(db, preview_id)
                if reused is not None:
                    log.info("job %s reuses preview %s file", job_id, preview_id)
            source = (
                reused
                if reused is not None
                else _download_with_chain(providers, url, start, end)
            )
            if reused is None:
                tmp_to_cleanup.append(source.path)

        # --- actual-duration enforcement (spec: "where determinable") ------
        end_effective = end
        clamped = False
        if source.duration is not None:
            available_end = source.segment_start + float(source.duration)
            if start >= available_end - 0.05:
                _fail(
                    db,
                    job_id,
                    f"Requested start time is beyond the end of the video "
                    f"(video is about {format_timecode(available_end)} long).",
                )
                return
            if end > available_end + 0.05:
                end_effective = available_end
                clamped = True
                log.info("job %s: end clamped to actual video length %s", job_id, available_end)

        with db.session() as session:
            job = repo.get_job(session, job_id)
            job.status = JobStatus.CLIPPING
            job.video_title = source.title
            job.provider = source.provider
            job.updated_at = _now()
            session.commit()

        style = get_style(style_id)
        if style is None:  # pragma: no cover - guarded at creation time
            _fail(db, job_id, f"Style {style_id!r} disappeared from the registry.")
            return

        output = settings.clips_dir / f"{job_id}.mp4"
        style.apply(
            source.path,
            output,
            start_in_source=start - source.segment_start,
            duration=end_effective - start,
            params=style_params,
        )
        if not output.exists() or output.stat().st_size == 0:
            _fail(db, job_id, "Clip style produced no output file.")
            return

        duration_out = ffprobe_duration(output) or (end_effective - start)
        notes = None
        if reused is not None:
            notes = "Source was reused from the cached preview download."
        if clamped:
            clamp_note = (
                "End time was clamped to the actual video length "
                f"({format_timecode(end_effective)})."
            )
            notes = f"{notes} {clamp_note}" if notes else clamp_note

        with db.session() as session:
            job = repo.get_job(session, job_id)
            job.status = JobStatus.COMPLETED
            job.error = None
            job.notes = notes
            job.output_filename = output.name
            job.output_size_bytes = output.stat().st_size
            job.output_duration_seconds = duration_out
            job.updated_at = _now()
            session.commit()
        log.info(
            "job %s completed: %s bytes, %.1fs",
            job_id,
            output.stat().st_size,
            duration_out,
        )
    except Exception as exc:
        log.exception("job %s errored during processing", job_id)
        message = str(exc) or repr(exc)
        _fail(db, job_id, message[:_MAX_ERROR_CHARS])
    finally:
        for path in tmp_to_cleanup:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                log.warning("could not remove temp file %s", path)
