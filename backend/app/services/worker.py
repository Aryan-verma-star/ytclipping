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

from app.config import Settings
from app.db.base import Database
from app.db import repo
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
                process_job(job_id, self.db, self.settings, self.providers)
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
