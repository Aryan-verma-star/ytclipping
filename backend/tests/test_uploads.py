"""Upload endpoint + uploaded-file clip jobs.

Covers the full server-engine upload path: multipart POST /api/uploads
(streamed to disk, ffprobe-validated), job creation with upload_id, the
orchestrator cutting the 9:16 clip from the uploaded bytes, and the
retention sweep. The provider chain is irrelevant here — uploaded sources
bypass providers entirely (empty chain passed where needed).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.ffmpeg import run_ffmpeg
from app.services.retention import sweep_once
from app.services.uploads import sweep_uploads
from tests.conftest import make_settings, run_job


@pytest.fixture(scope="module")
def small_video(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("uploadsrc") / "my holiday video.mp4"
    run_ffmpeg(
        [
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", "4",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-c:a", "aac", "-b:a", "64k", "-shortest",
            str(path),
        ]
    )
    return path


def _upload(client, path: Path, name: str | None = None):
    real_name = name or path.name
    with open(path, "rb") as fh:
        return client.post(
            "/api/uploads",
            files={"file": (real_name, fh, "video/mp4")},
        )


def _create_upload_job(client, uid: str, start="0", end="2"):
    return client.post(
        "/api/jobs",
        json={
            "upload_id": uid,
            "start_time": start,
            "end_time": end,
            "style_id": "original",
            "style_params": {},
        },
    )


# ------------------------------- the endpoint -------------------------------


def test_upload_returns_probed_metadata(client, settings, small_video):
    resp = _upload(client, small_video)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["filename"] == "my holiday video.mp4"
    assert body["duration"] == pytest.approx(4.0, abs=0.5)
    assert body["width"] == 320 and body["height"] == 180
    # sidecar + binary landed on disk next to each other
    uid = body["id"]
    assert (settings.uploads_dir / f"{uid}.bin").is_file()
    meta = json.loads((settings.uploads_dir / f"{uid}.json").read_text())
    assert meta["filename"] == "my holiday video.mp4"


def test_upload_rejects_non_video(client, settings):
    resp = client.post(
        "/api/uploads",
        files={"file": ("notes.txt", b"definitely not a video " * 100, "text/plain")},
    )
    assert resp.status_code == 422
    assert "video" in resp.json()["error"]["message"].lower()
    # no leftovers: the streamed bytes were deleted again
    assert list(settings.uploads_dir.glob("*.bin")) == []


def test_upload_rejects_empty_file(client):
    resp = client.post(
        "/api/uploads", files={"file": ("empty.mp4", b"", "video/mp4")}
    )
    assert resp.status_code == 422


def test_upload_enforces_size_cap(tmp_path, small_video):
    from fastapi.testclient import TestClient

    from app.main import create_app

    tiny = make_settings(tmp_path / "sizecap", max_upload_bytes=10)
    with TestClient(create_app(tiny)) as c:
        resp = _upload(c, small_video)
        assert resp.status_code == 413
        assert "larger than" in resp.json()["error"]["message"]


def test_upload_bypasses_the_tiny_general_request_cap(tmp_path, small_video):
    """The middleware's 64 KB JSON-body cap must not block multipart uploads."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    strict = make_settings(tmp_path / "strictcap", max_request_bytes=100)
    with TestClient(create_app(strict)) as c:
        resp = _upload(c, small_video)
        assert resp.status_code == 200, resp.text


def test_get_upload_metadata_roundtrip(client, small_video):
    uid = _upload(client, small_video).json()["id"]
    resp = client.get(f"/api/uploads/{uid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == uid


def test_get_unknown_upload_404(client):
    resp = client.get("/api/uploads/" + "0" * 32)
    assert resp.status_code == 404
    # path-traversal shapes are rejected the same way
    assert client.get("/api/uploads/..%2f..%2fetc").status_code == 404


# ------------------------------ job integration ------------------------------


def test_upload_job_clips_server_side(client, settings, small_video):
    uid = _upload(client, small_video).json()["id"]
    resp = _create_upload_job(client, uid)
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["source_url"] == f"upload://{uid}"
    assert job["video_title"] == "my holiday video.mp4"
    assert job["status"] == "queued"

    run_job(client, job["id"])

    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["status"] == "completed", done["error"]
    assert done["provider"] == "upload"
    assert done["output_size_bytes"] > 0
    clip = client.get(f"/api/jobs/{job['id']}/clip")
    assert clip.status_code == 200
    assert clip.headers["content-type"].startswith("video/mp4")
    assert len(clip.content) == done["output_size_bytes"]


def test_upload_job_fails_cleanly_when_file_vanishes(client, settings, small_video):
    uid = _upload(client, small_video).json()["id"]
    job = _create_upload_job(client, uid).json()
    # simulate the retention sweep removing the pair before the job ran
    (settings.uploads_dir / f"{uid}.bin").unlink()
    run_job(client, job["id"])
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["status"] == "failed"
    assert "no longer available" in done["error"]


def test_job_requires_exactly_one_source(client, small_video):
    uid = _upload(client, small_video).json()["id"]
    # neither url nor upload_id
    resp = client.post(
        "/api/jobs",
        json={"start_time": "0", "end_time": "2", "style_id": "original"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["field"] == "url"
    # both at once
    resp = client.post(
        "/api/jobs",
        json={
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "upload_id": uid,
            "start_time": "0",
            "end_time": "2",
            "style_id": "original",
        },
    )
    assert resp.status_code == 422
    # unknown upload id
    resp = _create_upload_job(client, "f" * 32)
    assert resp.status_code == 422
    assert resp.json()["error"]["field"] == "upload_id"


# -------------------------------- retention ---------------------------------


def test_sweep_uploads_removes_expired_pairs(client, settings, small_video):
    uid = _upload(client, small_video).json()["id"]
    bin_path = settings.uploads_dir / f"{uid}.bin"
    meta_path = settings.uploads_dir / f"{uid}.json"
    assert bin_path.is_file() and meta_path.is_file()

    # age the pair past the retention window (mtime drives the sweep)
    old = time.time() - 25 * 3600
    os.utime(bin_path, (old, old))
    os.utime(meta_path, (old, old))

    removed = sweep_uploads(settings, now=datetime.now(timezone.utc))
    assert removed >= 1
    assert not bin_path.exists() and not meta_path.exists()


def test_sweep_once_includes_uploads(client, settings, small_video):
    uid = _upload(client, small_video).json()["id"]
    meta_path = settings.uploads_dir / f"{uid}.json"
    old = time.time() - 25 * 3600
    os.utime(meta_path, (old, old))
    sweep_once(client.app.state.db, settings)
    assert not meta_path.exists()
