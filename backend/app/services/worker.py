"""In-process job worker.

Free-tier reality (feasibility report §3): Render Free has no free background
worker, so jobs are processed by a single daemon thread inside the web
service. Concurrency is deliberately 1 (512 MB RAM ceiling). If the service
restarts mid-job, startup recovery marks the job failed with a clear message
— history stays truthful.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from app.config import Settings
from app.db.base import Database
from app.db import repo
from app.db.models import JobStatus
from app.downloader.base import DownloaderProvider
from app.services.orchestrator import process_job

log = logging.getLogger("clipper.worker")


class JobWorker(threading.Thread):
    def __init__(
        self,
        db: Database,
        settings: Settings,
        providers: list[DownloaderProvider],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="clipper-job-worker")
        self.db = db
        self.settings = settings
        self.providers = providers
        self.stop_event = stop_event
        # supervision hooks: what this worker is inside right now (None = idle)
        self.current_id: str | None = None
        self.current_since: float | None = None

    def run(self) -> None:
        log.info("job worker started (providers: %s)", [p.name for p in self.providers])
        while not self.stop_event.is_set():
            try:
                with self.db.session() as session:
                    # Runnable = queued jobs whose preview (if any) is READY —
                    # jobs created against a still-downloading preview wait
                    # here and are picked up the moment the cache lands.
                    job_id = repo.oldest_runnable_job_id(session)
                if job_id is None:
                    self.stop_event.wait(self.settings.worker_poll_interval_seconds)
                    continue
                self.current_id = job_id
                self.current_since = time.monotonic()
                try:
                    process_job(job_id, self.db, self.settings, self.providers)
                finally:
                    self.current_id = None
                    self.current_since = None
            except Exception:  # pragma: no cover - the loop must never die
                log.exception("worker loop iteration failed")
                self.stop_event.wait(self.settings.worker_poll_interval_seconds)
        log.info("job worker stopped")

    def stop(self) -> None:
        self.stop_event.set()


def recover_stale_jobs(db: Database) -> int:
    """Startup recovery: jobs interrupted by a restart/redeploy become failed."""
    with db.session() as session:
        count = repo.fail_stale_jobs(
            session,
            "Service restarted while this job was being processed (free-tier "
            "services restart on redeploy or spin-down). Please submit it again.",
        )
    if count:
        log.warning("recovered %d stale job(s) as failed", count)
    return count


def fail_stuck_job(db: Database, job_id: str, message: str | None = None) -> None:
    """Supervisor hook: a job stuck in one worker for too long becomes failed.

    The worker thread itself is replaced by the supervisor — this only fixes
    the row so the UI stops waiting and shows an actionable message.
    """
    text = message or (
        "This clip took unusually long to process (the server was busy or the "
        "source too heavy) and was stopped. Please try again — shorter clips "
        "process faster."
    )
    with db.session() as session:
        job = repo.get_job(session, job_id)
        if job is None or job.status not in (JobStatus.QUEUED, JobStatus.DOWNLOADING, JobStatus.CLIPPING):
            return  # already finished/failed — nothing to fix
        job.status = JobStatus.FAILED
        job.error = text
        job.updated_at = datetime.now(timezone.utc)
        session.commit()
    log.error("job %s failed by the supervisor (stuck worker)", job_id)
