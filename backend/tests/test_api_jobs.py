"""API + job lifecycle tests with the (offline) sample provider.

Covers: creation validation, full happy path, failure path, history with
pagination, clip serving with Range, rate limiting, request-size guard,
styles/health/meta endpoints, and the AI stub.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AlwaysFailProvider, make_settings, post_job, run_job
from app.main import create_app

VALID_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


# ---------------- creation & validation ----------------

def test_create_job_accepted(client):
    resp = post_job(client, start="0:02", end="0:07")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["video_id"] == "jNQXAC9IVRw"
    assert body["source_url"] == VALID_URL
    assert body["start_seconds"] == 2.0
    assert body["end_seconds"] == 7.0
    assert body["start_timecode"] == "00:00:02"
    assert body["end_timecode"] == "00:00:07"
    assert body["style_id"] == "original"
    assert body["clip_url"] is None  # not completed yet
    assert body["created_at"]


def test_create_job_accepts_plain_seconds_and_timecodes(client):
    resp = post_job(client, start=90, end="0:02:30")
    assert resp.status_code == 202
    body = resp.json()
    assert body["start_seconds"] == 90.0
    assert body["end_seconds"] == 150.0


def test_create_job_accepts_schemeless_short_url(client):
    resp = post_job(client, url="youtu.be/aBcDeFgHiJk")
    assert resp.status_code == 202


@pytest.mark.parametrize(
    "payload_mutator,expect_fragment",
    [
        (lambda p: p.update(url="https://vimeo.com/1"), "YouTube"),
        (lambda p: p.update(start_time="0:10", end_time="0:05"), "earlier"),
        (lambda p: p.update(start_time="0:10", end_time="0:10"), "earlier"),
        (lambda p: p.update(start_time="0", end_time="700"), "maximum"),
        (lambda p: p.update(start_time="13801", end_time="14401"), "source"),
        (lambda p: p.update(style_id="nonexistent"), "style"),
        (lambda p: p.update(start_time="banana"), "time"),
        (lambda p: p.update(start_time="-5", end_time="10"), "negative"),
    ],
)
def test_create_job_rejects_invalid_input(client, payload_mutator, expect_fragment):
    payload = {
        "url": VALID_URL,
        "start_time": "0:02",
        "end_time": "0:07",
        "style_id": "original",
    }
    payload_mutator(payload)
    resp = client.post("/api/jobs", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "validation_error"
    assert expect_fragment.lower() in body["error"]["message"].lower()


def test_create_job_rejects_unknown_style_params(client):
    resp = post_job(client, style_params={"bogus": True})
    assert resp.status_code == 422
    details = resp.json()["error"]["details"]
    assert any(d["param"] == "bogus" for d in details)


def test_missing_body_fields_rejected(client):
    resp = client.post("/api/jobs", json={"url": VALID_URL})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


# ---------------- lifecycle: happy path (sample provider) ----------------

def test_full_lifecycle_completed(client):
    created = post_job(client, start="0:02", end="0:07")
    job_id = created.json()["id"]

    run_job(client, job_id)

    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "completed"
    assert detail["provider"] == "sample"
    assert detail["video_title"]  # sample provider reports a title
    assert detail["error"] is None
    assert detail["output_size_bytes"] > 0
    assert 4.5 <= detail["output_duration_seconds"] <= 5.5
    assert detail["clip_url"] == f"/api/jobs/{job_id}/clip"
    assert detail["download_url"] == f"/api/jobs/{job_id}/clip?download=1"


def test_clip_stream_and_download(client):
    created = post_job(client, start="0:01", end="0:05")
    job_id = created.json()["id"]
    run_job(client, job_id)

    stream = client.get(f"/api/jobs/{job_id}/clip")
    assert stream.status_code == 200
    assert stream.headers["content-type"] == "video/mp4"
    assert len(stream.content) > 1000

    download = client.get(f"/api/jobs/{job_id}/clip?download=1")
    assert download.status_code == 200
    assert "attachment" in download.headers.get("content-disposition", "")
    assert ".mp4" in download.headers.get("content-disposition", "")

    # Range requests → 206 partial content (video seeking)
    ranged = client.get(f"/api/jobs/{job_id}/clip", headers={"Range": "bytes=0-1023"})
    assert ranged.status_code == 206
    assert "content-range" in ranged.headers
    assert len(ranged.content) == 1024


def test_clip_not_ready_conflict(client):
    created = post_job(client)
    job_id = created.json()["id"]
    resp = client.get(f"/api/jobs/{job_id}/clip")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "clip_not_ready"


# ---------------- lifecycle: failure path ----------------

def test_failed_job_records_clear_error(tmp_path):
    app = create_app(make_settings(tmp_path))
    app.state.providers = [AlwaysFailProvider()]
    with TestClient(app) as failing_client:
        created = post_job(failing_client)
        job_id = created.json()["id"]
        run_job(failing_client, job_id)

        detail = failing_client.get(f"/api/jobs/{job_id}").json()
        assert detail["status"] == "failed"
        assert "always_fail" in detail["error"]
        assert "injected failure" in detail["error"]

        # failed jobs appear in history too (spec §12)
        history = failing_client.get("/api/jobs").json()
        assert history["total"] == 1
        assert history["items"][0]["status"] == "failed"


def test_start_beyond_sample_video_duration_fails_cleanly(client):
    # the sample provider synthesizes at most 120s; start=2h is far beyond it
    created = post_job(client, start="2:00:00", end="2:00:10")
    job_id = created.json()["id"]
    run_job(client, job_id)
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "failed"
    assert "beyond" in detail["error"]


def test_end_clamped_to_available_video(client):
    # request a window ending past the synthesized 120s cap → end gets clamped
    created = post_job(client, start="0:01:40", end="0:02:10")
    job_id = created.json()["id"]
    run_job(client, job_id)
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "completed"
    assert detail["notes"]
    assert "clamped" in detail["notes"].lower()
    assert detail["output_duration_seconds"] < 30.0


# ---------------- history ----------------

def test_history_pagination_and_ordering(client):
    ids = [post_job(client, end="0:0%d" % (i + 3)).json()["id"] for i in range(3)]

    page1 = client.get("/api/jobs", params={"limit": 2, "offset": 0}).json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert page1["limit"] == 2 and page1["offset"] == 0

    page2 = client.get("/api/jobs", params={"limit": 2, "offset": 2}).json()
    assert len(page2["items"]) == 1

    # newest first
    assert page1["items"][0]["id"] == ids[-1]
    assert page1["items"][1]["id"] == ids[-2]
    assert page2["items"][0]["id"] == ids[0]

    # history rows carry URL + timings as the spec requires
    item = page1["items"][0]
    assert item["source_url"] == VALID_URL
    assert item["start_timecode"] and item["end_timecode"]
    assert item["created_at"]


def test_history_status_filter(client):
    done_id = post_job(client, start="0:01", end="0:04").json()["id"]
    post_job(client, start="0:01", end="0:05")
    run_job(client, done_id)

    completed = client.get("/api/jobs", params={"status": "completed"}).json()
    queued = client.get("/api/jobs", params={"status": "queued"}).json()
    assert completed["total"] == 1
    assert completed["items"][0]["id"] == done_id
    assert queued["total"] == 1

    bogus = client.get("/api/jobs", params={"status": "wat"})
    assert bogus.status_code == 422


def test_get_unknown_job_404(client):
    resp = client.get("/api/jobs/doesnotexist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# ---------------- auxiliary endpoints ----------------

def test_styles_endpoint(client):
    resp = client.get("/api/styles")
    assert resp.status_code == 200
    styles = resp.json()
    original = next(s for s in styles if s["id"] == "original")
    assert original["name"] == "Original"
    assert original["parameters"][0]["name"] == "background"
    assert original["parameters"][0]["choices"] == ["blur", "black"]


def test_health_endpoint(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["ffmpeg"] == "ok"
    assert "sample" in body["provider_chain"]


def test_meta_endpoint_exposes_limits_and_notice(client):
    resp = client.get("/api/meta")
    assert resp.status_code == 200
    meta = resp.json()
    assert meta["limits"]["max_clip_seconds"] == 600
    assert meta["limits"]["max_clip_timecode"] == "00:10:00"
    assert meta["notice"]  # ToS notice (spec §11)
    assert meta["retention_hours"] == 24


def test_ai_stub_returns_501_with_docs_pointer(client):
    resp = client.post("/api/ai/suggest", json={"url": VALID_URL})
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "not_implemented"
    assert "analyzer" in resp.json()["error"]["message"].lower()


def test_delete_job_file_keeps_record(client):
    created = post_job(client, start="0:01", end="0:04")
    job_id = created.json()["id"]
    run_job(client, job_id)

    deleted = client.delete(f"/api/jobs/{job_id}")
    assert deleted.status_code == 200
    body = deleted.json()
    assert body["file_deleted_at"]

    gone = client.get(f"/api/jobs/{job_id}/clip")
    assert gone.status_code == 410
    assert gone.json()["error"]["code"] == "clip_expired"

    # metadata record still there
    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "completed"


# ---------------- abuse protections ----------------

def test_rate_limit_on_job_creation(tmp_path):
    app = create_app(make_settings(tmp_path, rate_limit_jobs_per_minute=2))
    with TestClient(app) as tight:
        assert post_job(tight).status_code == 202
        assert post_job(tight).status_code == 202
        third = post_job(tight)
        assert third.status_code == 429
        assert third.json()["error"]["code"] == "rate_limited"
        assert "Retry-After" in third.headers or "retry-after" in third.headers


def test_request_size_guard(tmp_path):
    app = create_app(make_settings(tmp_path, max_request_bytes=200))
    with TestClient(app) as small:
        big_payload = {"url": VALID_URL, "start_time": "0:01", "end_time": "0:05", "junk": "x" * 5000}
        resp = small.post("/api/jobs", json=big_payload)
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "payload_too_large"


def test_invalid_json_body_handled(client):
    resp = client.post(
        "/api/jobs",
        content=json.dumps({"url": VALID_URL, "start_time": "0:01", "end_time": "0:05"})[:-2],
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
