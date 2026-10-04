"""Persistence tests (spec §7, success criterion §12):

- Records survive an application restart (new app instance, same DB).
- Retention sweeper deletes files but keeps metadata (file_deleted_at).
- Startup recovery marks interrupted jobs as failed.
- Alembic migration is the schema-creation mechanism.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.base import Database
from app.db import repo
from app.db.models import JobStatus
from app.main import create_app
from app.services.retention import sweep_once
from app.services.worker import recover_stale_jobs
from tests.conftest import make_settings, post_job, run_job


def test_records_survive_app_restart(tmp_path):
    settings = make_settings(tmp_path)

    # instance 1: create two jobs, complete one, then "restart"
    with TestClient(create_app(settings)) as first:
        done_id = post_job(first, start="0:01", end="0:04").json()["id"]
        queued_id = post_job(first, start="0:02", end="0:06").json()["id"]
        run_job(first, done_id)

    # instance 2: same database file, brand-new app objects
    with TestClient(create_app(settings)) as second:
        history = second.get("/api/jobs").json()
        assert history["total"] == 2

        done = second.get(f"/api/jobs/{done_id}").json()
        queued = second.get(f"/api/jobs/{queued_id}").json()
        assert done["status"] == "completed"
        assert queued["status"] == "queued"
        assert done["clip_url"], "completed clip must remain servable after restart"
        assert second.get(done["clip_url"]).status_code == 200


def test_retention_sweeper_deletes_files_keeps_metadata(client, settings):
    created = post_job(client, start="0:01", end="0:04")
    job_id = created.json()["id"]
    run_job(client, job_id)

    job = client.get(f"/api/jobs/{job_id}").json()
    clip_path = settings.clips_dir / f"{job_id}.mp4"
    assert clip_path.exists()

    # backdate created_at beyond the retention window
    with client.app.state.db.session() as session:
        session.execute(
            text("UPDATE jobs SET created_at = :past WHERE id = :id"),
            {"past": datetime.now(timezone.utc) - timedelta(days=3), "id": job_id},
        )
        session.commit()

    removed = sweep_once(client.app.state.db, settings)
    assert removed == 1
    assert not clip_path.exists()

    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "completed"  # metadata kept (spec §7)
    assert detail["file_deleted_at"]
    assert client.get(f"/api/jobs/{job_id}/clip").status_code == 410

    # sweeping again is a no-op
    assert sweep_once(client.app.state.db, settings) == 0


def _insert_raw_job(db, job_id: str, source_url: str, status: str):
    with db.session() as session:
        session.execute(
            text(
                "INSERT INTO jobs (id, source_url, video_id, start_seconds, end_seconds,"
                " style_id, style_params, status) VALUES"
                f" ('{job_id}', '{source_url}', 'jNQXAC9IVRw',"
                f" 1.0, 5.0, 'original', '{{}}', '{status}')"
            )
        )
        session.commit()


def test_startup_recovery_requeues_refetchable_jobs_once(tmp_path):
    """A restart mid-clip no longer kills the job: refetchable (http) sources
    are requeued ONCE — the provider chain re-downloads and the clip renders
    on the restarted backend. The second interruption fails it for real."""
    settings = make_settings(tmp_path)
    db = Database(settings.resolved_database_url)
    db.create_all()

    # three flavors of interrupted work
    _insert_raw_job(db, "stale1", "https://www.youtube.com/watch?v=jNQXAC9IVRw", "clipping")
    _insert_raw_job(db, "stale2", "upload://abcdef0123456789", "clipping")  # not refetchable
    _insert_raw_job(db, "stale3", "https://www.youtube.com/watch?v=jNQXAC9IVRw", "downloading")

    recovered = recover_stale_jobs(db)
    assert recovered == 3

    with db.session() as session:
        j1 = repo.get_job(session, "stale1")
        assert j1.status == JobStatus.QUEUED  # requeued…
        assert j1.restart_retries == 1
        assert j1.preview_id is None  # dead preview reference dropped
        assert "retrying" in (j1.notes or "").lower()
        j2 = repo.get_job(session, "stale2")
        assert j2.status == JobStatus.FAILED  # upload died with its file
        assert "restarted" in j2.error.lower()
        j3 = repo.get_job(session, "stale3")
        assert j3.status == JobStatus.QUEUED and j3.restart_retries == 1

    # a SECOND restart catches stale1 again — retry budget spent, fail it
    with db.session() as session:
        session.execute(text("UPDATE jobs SET status = 'clipping' WHERE id = 'stale1'"))
        session.commit()
    recover_stale_jobs(db)
    with db.session() as session:
        j1 = repo.get_job(session, "stale1")
        assert j1.status == JobStatus.FAILED
        assert "retried this clip automatically once" in j1.error
    db.dispose()


def test_schema_created_by_alembic_migration(tmp_path):
    """init_db_on_startup=True path: tables must exist via migration, not create_all."""
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as app_client:
        with app_client.app.state.db.session() as session:
            tables = {
                row[0]
                for row in session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            }
    assert "jobs" in tables
    assert "alembic_version" in tables, "alembic must own the schema version"


def test_worker_thread_processes_queue(tmp_path):
    """End-to-end through the real background worker (poll → process)."""
    settings = make_settings(tmp_path, worker_enabled=True, worker_poll_interval_seconds=0.1)
    with TestClient(create_app(settings)) as live:
        job_id = post_job(live, start="0:01", end="0:04").json()["id"]

        deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
        status = "queued"
        while datetime.now(timezone.utc) < deadline:
            status = live.get(f"/api/jobs/{job_id}").json()["status"]
            if status in ("completed", "failed"):
                break
        assert status == "completed", live.get(f"/api/jobs/{job_id}").json()
