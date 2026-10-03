"""Preview pipeline tests (timeline UI backend).

All offline: the sample provider synthesizes the "source video". Tests drive
process_preview() directly (worker disabled), exactly like the job tests.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import repo
from app.db.models import PreviewStatus
from app.downloader.base import StreamTarget
from app.services.preview import process_preview, recover_stale_previews
from app.services.retention import sweep_once
from tests.conftest import make_settings, post_job, run_job

VALID_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
OTHER_URL = "https://youtu.be/aqz-KE-bpKQ"


def make_preview_settings(tmp_path: Path, **overrides):
    base = dict(
        sample_max_seconds=24.0,  # short syntheses keep the suite fast
        preview_thumb_count=8,
        preview_thumb_height=96,
    )
    base.update(overrides)
    return make_settings(tmp_path, **base)


# module-scoped overrides of the shared fixtures: fast sample + few thumbs
@pytest.fixture()
def settings(tmp_path: Path):
    return make_preview_settings(tmp_path)


@pytest.fixture()
def app(settings):
    from app.main import create_app

    return create_app(settings)


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


def create_preview(client, url: str = VALID_URL):
    return client.post("/api/previews", json={"url": url})


def run_preview(client, preview_id: str):
    process_preview(
        preview_id, client.app.state.db, client.app.state.settings, client.app.state.providers
    )


@pytest.fixture()
def ready_preview(client):
    """A processed, READY preview for VALID_URL."""
    resp = create_preview(client)
    assert resp.status_code == 202
    preview_id = resp.json()["id"]
    run_preview(client, preview_id)
    return preview_id


# ---------------- creation + validation ----------------


def test_create_preview_rejects_non_youtube_url(client):
    resp = client.post("/api/previews", json={"url": "https://vimeo.com/12345"})
    assert resp.status_code == 422
    assert resp.json()["error"]["field"] == "url"


def test_create_preview_returns_202_pending(client):
    resp = create_preview(client)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert body["video_id"] == "jNQXAC9IVRw"
    assert body["video_url"] is None  # no files until ready


def test_get_unknown_preview_404(client):
    resp = client.get("/api/previews/doesnotexist")
    assert resp.status_code == 404


# ---------------- the happy path ----------------


def test_preview_ready_flow(client, ready_preview):
    resp = client.get(f"/api/previews/{ready_preview}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["provider"] == "sample"
    # sample provider synthesizes min(max_source+2, sample_max) = 24s
    assert 22.0 <= body["duration"] <= 25.0
    assert body["width"] and body["height"]
    assert body["expires_at"]
    assert len(body["thumbs"]) == body["thumb_count"] == 8
    assert body["video_url"] == f"/api/previews/{ready_preview}/stream"
    assert body["progress"] == 1.0
    # thumb URLs point at this preview
    assert all(t.startswith(f"/api/previews/{ready_preview}/thumbs/") for t in body["thumbs"])


def test_preview_thumbnails_serve_jpeg(client, ready_preview):
    listing = client.get(f"/api/previews/{ready_preview}").json()
    for thumb_url in listing["thumbs"]:
        resp = client.get(thumb_url)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content[:2] == b"\xff\xd8"  # JPEG magic


def test_preview_thumbnail_name_validation(client, ready_preview):
    assert client.get(f"/api/previews/{ready_preview}/thumbs/000.jpg").status_code == 200
    # path traversal / bogus names → 404, never a filesystem error
    for bad in ("999.jpg", "abc.jpg", "000.png", "..%2F000.jpg", "000.jpg%00"):
        resp = client.get(f"/api/previews/{ready_preview}/thumbs/{bad}")
        assert resp.status_code == 404, bad


def test_preview_video_streams_with_range_support(client, ready_preview):
    # the /stream endpoint is the single playback URL for the preview's life
    for url in (f"/api/previews/{ready_preview}/video", f"/api/previews/{ready_preview}/stream"):
        full = client.get(url)
        assert full.status_code == 200
        assert full.headers["content-type"] == "video/mp4"
        assert len(full.content) > 1000

        ranged = client.get(url, headers={"Range": "bytes=0-99"})
        assert ranged.status_code == 206
        assert len(ranged.content) == 100
        assert ranged.headers["content-range"].startswith("bytes 0-99/")


def test_preview_dedupe_returns_existing_ready_preview(client, ready_preview):
    resp = create_preview(client)  # same URL
    assert resp.status_code == 202
    assert resp.json()["id"] == ready_preview
    assert resp.json()["status"] == "ready"


# ---------------- job reuse via preview_id ----------------


def test_job_reuses_preview_download(client, ready_preview):
    providers = client.app.state.providers
    calls = []
    original_get_video = providers[0].get_video

    def counting_get_video(url, start, end, progress=None, **kwargs):
        calls.append((url, start, end))
        return original_get_video(url, start, end, progress=progress, **kwargs)

    providers[0].get_video = counting_get_video
    try:
        assert len(calls) == 0
        resp = post_job(client, start="0:02", end="0:07", preview_id=ready_preview)
        assert resp.status_code == 202
        run_job(client, resp.json()["id"])
        # the preview's single download is the only one — no second fetch
        assert len(calls) == 0
    finally:
        providers[0].get_video = original_get_video

    job = client.get(f"/api/jobs/{resp.json()['id']}").json()
    assert job["status"] == "completed"
    assert job["provider"] == "sample"
    assert job["notes"] and "preview" in job["notes"].lower()

    # the preview itself is untouched and still servable
    assert client.get(f"/api/previews/{ready_preview}").json()["status"] == "ready"
    assert client.get(f"/api/previews/{ready_preview}/video").status_code == 200


def test_job_preview_id_must_match_url(client, ready_preview):
    resp = post_job(client, url=OTHER_URL, start="0:01", end="0:05", preview_id=ready_preview)
    assert resp.status_code == 422
    assert resp.json()["error"]["field"] == "preview_id"


def test_job_accepts_inflight_preview_and_waits(client):
    """The real-time flow: create the clip while the download is still running.

    The job is accepted (202) but not runnable until the preview's file lands;
    afterwards it completes reusing the cached download (provider hit once).
    """
    from app.db import repo as repo_mod

    providers = client.app.state.providers
    calls = []
    original_get_video = providers[0].get_video

    def counting_get_video(url, start, end, progress=None, **kwargs):
        calls.append(1)
        return original_get_video(url, start, end, progress=progress, **kwargs)

    providers[0].get_video = counting_get_video
    try:
        resp = create_preview(client)
        preview_id = resp.json()["id"]  # PENDING — never processed yet

        job_resp = post_job(client, start="0:01", end="0:05", preview_id=preview_id)
        assert job_resp.status_code == 202
        job_id = job_resp.json()["id"]

        db = client.app.state.db
        with db.session() as session:
            assert repo_mod.oldest_runnable_job_id(session) is None  # waiting

        run_preview(client, preview_id)  # background download completes

        with db.session() as session:
            assert repo_mod.oldest_runnable_job_id(session) == job_id  # runnable
        run_job(client, job_id)

        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "completed"
        assert job["notes"] and "preview" in job["notes"].lower()
        assert len(calls) == 1  # ONE download for the whole flow
    finally:
        providers[0].get_video = original_get_video


def test_preview_failure_fails_waiting_jobs(client):
    resp = create_preview(client)
    preview_id = resp.json()["id"]
    job_resp = post_job(client, start="0:01", end="0:05", preview_id=preview_id)
    assert job_resp.status_code == 202
    job_id = job_resp.json()["id"]

    # the preview fails while the job is queued and waiting on its file
    from app.db import repo as repo_mod

    db = client.app.state.db
    with db.session() as session:
        count = repo_mod.fail_jobs_waiting_on_preview(
            session, preview_id, "The background source download failed: provider outage."
        )
    assert count == 1
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert "background source download" in job["error"]


def test_job_preview_id_validation_states(client):
    # unknown id → 422
    unknown = post_job(client, start="0:01", end="0:05", preview_id="deadbeef" * 4)
    assert unknown.status_code == 422

    # failed preview → 422 with a clear message
    resp = create_preview(client)
    failed_id = resp.json()["id"]
    db = client.app.state.db
    with db.session() as session:
        preview = repo.get_preview(session, failed_id)
        preview.status = PreviewStatus.FAILED
        preview.error = "boom"
        session.commit()
    rejected = post_job(client, start="0:01", end="0:05", preview_id=failed_id)
    assert rejected.status_code == 422
    assert "failed" in rejected.json()["error"]["message"].lower()


def test_completed_clip_is_916_from_preview_source(client, ready_preview):
    """The user-facing promise: output is 1080x1920 with the video centered."""
    resp = post_job(client, start="0:02", end="0:06", preview_id=ready_preview)
    run_job(client, resp.json()["id"])
    job = client.get(f"/api/jobs/{resp.json()['id']}").json()
    assert job["status"] == "completed"
    clip = client.get(job["clip_url"])
    assert clip.status_code == 200
    # persist and probe dimensions
    from app.core.ffmpeg import ffprobe_video_info

    settings = client.app.state.settings
    tmp = Path(settings.tmp_dir) / "e2e_probe.mp4"
    tmp.write_bytes(clip.content)
    duration, width, height = ffprobe_video_info(tmp)
    assert (width, height) == (1080, 1920)
    assert 3.5 <= duration <= 4.5
    tmp.unlink(missing_ok=True)


# ---------------- failure + expiry paths ----------------


def test_preview_fails_when_source_exceeds_max(tmp_path):
    from app.main import create_app

    settings = make_preview_settings(tmp_path, max_source_seconds=5.0)
    app = create_app(settings)
    with TestClient(app) as c:
        resp = create_preview(c)
        preview_id = resp.json()["id"]
        run_preview(c, preview_id)
        body = c.get(f"/api/previews/{preview_id}").json()
        assert body["status"] == "failed"
        assert "longer than" in body["error"]

        # failed previews answer their file endpoints with a clean 409
        assert c.get(f"/api/previews/{preview_id}/video").status_code == 409


def test_preview_expiry_sweeps_files_and_returns_410(client, ready_preview):
    db = client.app.state.db
    with db.session() as session:
        preview = repo.get_preview(session, ready_preview)
        preview.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()

    removed = sweep_once(db, client.app.state.settings)
    assert removed >= 1

    body = client.get(f"/api/previews/{ready_preview}").json()
    assert body["status"] == "expired"
    assert client.get(f"/api/previews/{ready_preview}/video").status_code == 410
    assert client.get(f"/api/previews/{ready_preview}/thumbs/000.jpg").status_code == 410

    # files are gone from disk
    with db.session() as session:
        preview = repo.get_preview(session, ready_preview)
        if preview.file_path:
            assert not Path(preview.file_path).exists()
        if preview.thumb_dir:
            assert not Path(preview.thumb_dir).exists()


def test_stale_preview_recovery_at_startup(client, ready_preview):
    db = client.app.state.db
    with db.session() as session:
        preview = repo.get_preview(session, ready_preview)
        preview.status = PreviewStatus.PROCESSING
        session.commit()

    assert recover_stale_previews(db) == 1
    body = client.get(f"/api/previews/{ready_preview}").json()
    assert body["status"] == "failed"
    assert "restarted" in body["error"].lower()


def test_sweeper_fails_timed_out_previews(tmp_path):
    from app.main import create_app

    settings = make_preview_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as c:
        resp = create_preview(c)
        preview_id = resp.json()["id"]
        db = c.app.state.db
        with db.session() as session:
            preview = repo.get_preview(session, preview_id)
            preview.status = PreviewStatus.DOWNLOADING
            preview.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
            session.commit()

        sweep_once(db, c.app.state.settings)
        body = c.get(f"/api/previews/{preview_id}").json()
        assert body["status"] == "failed"
        assert "timed out" in body["error"].lower()


def test_expired_preview_not_deduped_for_new_previews(client, ready_preview):
    db = client.app.state.db
    with db.session() as session:
        preview = repo.get_preview(session, ready_preview)
        preview.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    sweep_once(db, client.app.state.settings)

    resp = create_preview(client)  # same URL, previous preview expired
    assert resp.status_code == 202
    assert resp.json()["id"] != ready_preview
    assert resp.json()["status"] == "pending"


# ---------------- media endpoints bypass the request counter ----------------


def test_media_endpoints_exempt_from_general_rate_limit(client, tmp_path):
    """A filmstrip (60 tiles) + video element (many Range GETs) must not
    exhaust the per-IP request bucket — otherwise the timeline UI 429s
    itself and the job poller loses track of a healthy job."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = make_preview_settings(tmp_path, rate_limit_per_minute=5)
    app = create_app(settings)
    with TestClient(app) as c:
        resp = c.post("/api/previews", json={"url": VALID_URL})
        assert resp.status_code == 202
        pid = resp.json()["id"]
        process_preview(pid, c.app.state.db, c.app.state.settings, c.app.state.providers)

        # exhaust the general bucket with non-media API calls (limit is 5/min)
        for _ in range(6):
            c.get("/api/meta")
        assert c.get("/api/meta").status_code == 429

        # media endpoints keep serving — they are exempt from the counter
        for _ in range(10):
            assert c.get(f"/api/previews/{pid}/video").status_code == 200
            assert c.get(f"/api/previews/{pid}/thumbs/000.jpg").status_code == 200


# ---------------- instant-load (stream-first) flow ----------------


class InstantProvider:
    """Fake provider with a resolvable direct stream, like cobalt.

    resolve_stream is instant; get_video "downloads" slowly and reports
    progress, letting tests observe the streaming→ready lifecycle.
    """

    name = "instant"
    supports_reuse = False

    def __init__(self, settings, *, delay_reports=None):
        self.settings = settings
        self.stream_url = "https://media.example/direct/file.mp4"
        self.delay_reports = delay_reports or []
        self.download_calls = 0

    def resolve_stream(self, url, start, end):
        return StreamTarget(
            provider=self.name,
            url=self.stream_url,
            headers={"User-Agent": "InstantProvider/1.0"},
            title="Instant test video",
            duration=30.0,
        )

    def get_video(self, url, start, end, progress=None):
        import time as _time

        self.download_calls += 1
        for fraction in self.delay_reports:
            if progress:
                progress(fraction)
            _time.sleep(0.02)
        if progress:
            progress(1.0)
        # synthesize a tiny real video so probing/thumbnails work
        from app.downloader.sample import SampleProvider

        return SampleProvider(self.settings).get_video(url, 0, 6)


def make_instant_app(tmp_path, **provider_kwargs):
    from app.main import create_app

    settings = make_preview_settings(tmp_path, sample_max_seconds=12.0)
    provider = InstantProvider(settings, **provider_kwargs)
    app = create_app(settings)
    app.state.providers = [provider]  # bypass the registry for this fake
    return app, provider


def test_instant_flow_shows_stream_before_download_completes(tmp_path):
    """Paste → duration + playable video_url appear while status=streaming."""
    import threading

    from fastapi.testclient import TestClient

    app, provider = make_instant_app(
        tmp_path, delay_reports=[0.25, 0.5, 0.75]
    )
    with TestClient(app) as c:
        db = c.app.state.db
        resp = c.post("/api/previews", json={"url": VALID_URL})
        preview_id = resp.json()["id"]

        observed = {}
        release = threading.Event()

        original_get_video = provider.get_video

        def blocking_get_video(url, start, end, progress=None):
            # capture the mid-download state the UI would poll
            observed["progress_snapshot"] = None
            for fraction in [0.3, 0.6]:
                if progress:
                    progress(fraction)
                r = c.get(f"/api/previews/{preview_id}")
                if fraction == 0.6:
                    observed["mid_body"] = r.json()
            release.wait(timeout=10)  # hold the "download" open
            return original_get_video(url, start, end, progress=progress)

        provider.get_video = blocking_get_video

        worker = threading.Thread(
            target=process_preview,
            args=(preview_id, db, c.app.state.settings, c.app.state.providers),
            daemon=True,
        )
        worker.start()

        deadline = time.time() + 10
        mid = None
        while time.time() < deadline:
            body = c.get(f"/api/previews/{preview_id}").json()
            if body.get("status") == "streaming":
                mid = body
                break
            time.sleep(0.05)
        assert mid is not None, f"never reached streaming (status={body['status']})"
        # the instant-load contract: playable + timeline metadata + progress
        assert mid["video_url"] == f"/api/previews/{preview_id}/stream"
        assert mid["duration"] == 30.0
        assert mid["title"] == "Instant test video"
        assert mid["stream_provider"] == "instant"
        assert 0.0 <= (mid["progress"] or 0) <= 1.0

        release.set()
        worker.join(timeout=30)

        final = c.get(f"/api/previews/{preview_id}").json()
        assert final["status"] == "ready"
        assert final["progress"] == 1.0
        assert final["duration"] == 8.0  # real probe replaces the 30.0 estimate
        assert provider.download_calls == 1


def test_stream_endpoint_proxies_upstream_with_range(tmp_path, monkeypatch):
    """/stream forwards Range to the provider URL and relays the 206."""
    import httpx
    from fastapi.testclient import TestClient

    app, provider = make_instant_app(tmp_path)
    payload = b"\x00" + b"FAKEVIDOEBYTES" * 512  # ~7.5 KB

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == provider.stream_url
        assert request.headers.get("User-Agent") == "InstantProvider/1.0"
        range_header = request.headers.get("Range")
        # NB: responses must be built with stream= (not content=) — a
        # content-built response counts as already-consumed for aiter_raw.
        if range_header:
            start_s, end_s = range_header.replace("bytes=", "").split("-")
            chunk = payload[int(start_s) : int(end_s) + 1]
            return httpx.Response(
                206,
                stream=httpx.ByteStream(chunk),
                headers={
                    "Content-Range": f"bytes {start_s}-{end_s}/{len(payload)}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(len(chunk)),
                },
            )
        return httpx.Response(
            200,
            stream=httpx.ByteStream(payload),
            headers={"Content-Length": str(len(payload)), "Accept-Ranges": "bytes"},
        )

    class FakeClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            super().__init__(transport=httpx.MockTransport(handler), *a, **kw)

    monkeypatch.setattr("app.api.routes_previews.httpx.AsyncClient", FakeClient)

    with TestClient(app) as c:
        db = c.app.state.db
        resp = c.post("/api/previews", json={"url": VALID_URL})
        preview_id = resp.json()["id"]
        # put the preview into the streaming state without downloading
        with db.session() as session:
            preview = repo.get_preview(session, preview_id)
            preview.status = PreviewStatus.STREAMING
            preview.stream_url = provider.stream_url
            preview.stream_headers = {"User-Agent": "InstantProvider/1.0"}
            preview.stream_provider = "instant"
            preview.duration = 30.0
            session.commit()

        full = c.get(f"/api/previews/{preview_id}/stream")
        assert full.status_code == 200
        assert full.content == payload

        ranged = c.get(
            f"/api/previews/{preview_id}/stream", headers={"Range": "bytes=0-99"}
        )
        assert ranged.status_code == 206
        assert len(ranged.content) == 100
        assert ranged.headers["content-range"] == f"bytes 0-99/{len(payload)}"


def test_stream_endpoint_409_while_nothing_playable(client):
    """Sample flow: no direct URL and no file yet → clean 409, not an error."""
    resp = create_preview(client)
    preview_id = resp.json()["id"]
    body = resp.json()
    assert body["status"] == "pending"
    assert body["video_url"] is None
    r = client.get(f"/api/previews/{preview_id}/stream")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "preview_not_playable"


def test_dedupe_returns_inflight_preview(client):
    """Pasting the same URL twice must not start a second download."""
    resp = create_preview(client)
    first = resp.json()
    assert first["status"] == "pending"

    second_resp = create_preview(client)
    second = second_resp.json()
    assert second_resp.status_code == 202
    assert second["id"] == first["id"]  # same in-flight preview


def test_progress_is_persisted_during_download(tmp_path):
    """The progress callback writes land in the DB mid-download."""
    from fastapi.testclient import TestClient

    app, provider = make_instant_app(tmp_path, delay_reports=[0.4])
    with TestClient(app) as c:
        db = c.app.state.db
        resp = c.post("/api/previews", json={"url": VALID_URL})
        preview_id = resp.json()["id"]

        seen = []
        original = provider.get_video

        def capturing(url, start, end, progress=None):
            if progress:
                progress(0.4)
                with db.session() as session:
                    preview = repo.get_preview(session, preview_id)
                    seen.append((preview.status, preview.progress))
            return original(url, start, end, progress=progress)

        provider.get_video = capturing
        process_preview(
            preview_id, db, c.app.state.settings, c.app.state.providers
        )
        assert seen and seen[0][0] == PreviewStatus.STREAMING
        assert seen[0][1] == pytest.approx(0.4)
