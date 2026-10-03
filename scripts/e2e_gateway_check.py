"""End-to-end verification of the running backend through the Caddy gateway.

Simulates exactly what the user's browser does via the preview panel:
every request carries ?XTransformPort=8000. Exercises the Phase 3 flow:
preview (timeline source + filmstrip) → clip job reusing the preview's file →
9:16 output with Range streaming. Sample provider keeps it offline.
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:81"
PORT_Q = "XTransformPort=8000"


def request(path, method="GET", body=None, headers=None, timeout=30):
    url = f"{BASE}{path}{'&' if '?' in path else '?'}{PORT_Q}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        sys.exit(1)


# 1. index.html served through the gateway with the bootstrap
status, _, body = request("/")
check("GET / serves the app UI", status == 200 and b"YouTube Clipper" in body and b"XTransformPort" in body)

# 2. static assets route to the backend
status, _, body = request("/styles.css")
check("GET /styles.css via gateway", status == 200 and b"card" in body)
status, _, body = request("/timeline.js")
check("GET /timeline.js via gateway", status == 200 and b"Timeline" in body)
status, _, body = request("/app.js")
check("GET /app.js via gateway", status == 200 and b"apiUrl" in body)

# 3. styles + meta endpoints
status, _, body = request("/api/styles")
styles = json.loads(body)
check("GET /api/styles", status == 200 and styles[0]["id"] == "original")
status, _, body = request("/api/meta")
meta = json.loads(body)
check("GET /api/meta", status == 200 and meta["limits"]["max_clip_seconds"] == 600)

# 3b. preview pipeline (timeline source): create → poll → ready
status, _, body = request(
    "/api/previews",
    method="POST",
    body={"url": "https://www.youtube.com/watch?v=e89W7N2jqoo"},
)
preview = json.loads(body)
check(
    "POST /api/previews → 202",
    status == 202 and preview["status"] in ("pending", "ready") and preview["id"],
    preview.get("id", ""),
)
preview_id = preview["id"]
if preview["status"] != "ready":
    deadline = time.time() + 240
    while time.time() < deadline:
        status, _, body = request(f"/api/previews/{preview_id}")
        preview = json.loads(body)
        if preview["status"] in ("ready", "failed", "expired"):
            break
        time.sleep(2)
check(
    "preview reaches ready",
    preview["status"] == "ready" and preview["duration"] > 0 and preview["thumbs"],
    f"{preview.get('duration')}s, {len(preview.get('thumbs') or [])} thumbs, provider={preview.get('provider')}",
)

status, headers, body = request(preview["thumbs"][0])
check("preview thumbnail serves JPEG", status == 200 and headers.get("Content-Type") == "image/jpeg" and body[:2] == b"\xff\xd8")
status, headers, body = request(preview["video_url"])
check("preview video streams", status == 200 and headers.get("Content-Type") == "video/mp4" and len(body) > 1000)
status, headers, body = request(preview["video_url"], headers={"Range": "bytes=0-1023"})
check("preview video Range → 206", status == 206 and len(body) == 1024)

# preview for the wrong URL must be rejected for job reuse
status, _, body = request(
    "/api/jobs",
    method="POST",
    body={
        "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "start_time": "1",
        "end_time": "2",
        "preview_id": preview_id,
    },
)
payload = json.loads(body)
check("preview_id URL mismatch rejected with 422", status == 422 and payload["error"]["field"] == "preview_id")

# 3c. INSTANT-LOAD flow: fresh URL → in-flight preview → job accepted while
# the background download runs → duration known before ready → /stream works.
# The &t= suffix keeps this URL unique per run: previews dedupe on the exact
# URL string for 24h, so re-running the E2E against the same database would
# otherwise return a cached READY preview and the in-flight checks could
# never observe the "duration before ready" state.
insta_url = f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&t={int(time.time())}"
status, _, body = request(
    "/api/previews",
    method="POST",
    body={"url": insta_url},
)
insta = json.loads(body)
check(
    "instant: POST /api/previews → 202",
    status == 202 and insta["id"] and insta["status"] in ("pending", "resolving", "streaming", "downloading", "ready"),
    insta.get("status", ""),
)
insta_id = insta["id"]

# the real-time contract: create the clip job while the preview is in-flight
job_inflight_body = {
    "url": insta_url,
    "start_time": "1",
    "end_time": "4",
    "style_id": "original",
    "preview_id": insta_id,
}
if insta["status"] == "ready":
    # synthesis already finished (cached) — the job is still accepted below
    pass
status, _, body = request("/api/jobs", method="POST", body=job_inflight_body)
inflight_job = json.loads(body)
check(
    "instant: job accepted against in-flight preview (202)",
    status == 202 and inflight_job["status"] == "queued",
    inflight_job.get("id", ""),
)

saw_duration_before_ready = insta["status"] != "ready" and insta.get("duration") is not None
insta_final = insta
deadline = time.time() + 300
while time.time() < deadline:
    status, _, body = request(f"/api/previews/{insta_id}")
    insta_final = json.loads(body)
    if insta_final["status"] in ("ready", "failed", "expired"):
        break
    if insta_final.get("duration"):
        saw_duration_before_ready = True
    time.sleep(1)
check("instant: preview reaches ready", insta_final["status"] == "ready", insta_final.get("error", ""))
check(
    "instant: duration was known BEFORE the download finished (timeline renders immediately)",
    saw_duration_before_ready and (insta_final.get("duration") or 0) > 0,
    f"duration={insta_final.get('duration')}",
)
status, headers, body = request(
    f"/api/previews/{insta_id}/stream", headers={"Range": "bytes=0-2047"}
)
check(
    "instant: /stream serves Range → 206",
    status == 206 and len(body) == 2048 and "Content-Range" in headers,
    f"status={status}",
)

# the queued job must complete once the cache lands (single download reused)
deadline = time.time() + 120
final_inflight = None
while time.time() < deadline:
    status, _, body = request(f"/api/jobs/{inflight_job['id']}")
    final_inflight = json.loads(body)
    if final_inflight["status"] in ("completed", "failed"):
        break
    time.sleep(2)
check(
    "instant: waiting job completes after background download",
    final_inflight["status"] == "completed",
    f"err={final_inflight.get('error')}",
)
check(
    "instant: waiting job reused the preview download",
    final_inflight.get("notes") and "preview" in final_inflight["notes"].lower(),
    final_inflight.get("notes", ""),
)

# 4. create a job that REUSES the preview's downloaded file
status, _, body = request(
    "/api/jobs",
    method="POST",
    body={
        "url": "https://www.youtube.com/watch?v=e89W7N2jqoo",
        "start_time": "0:02",
        "end_time": "0:07",
        "style_id": "original",
        "preview_id": preview_id,
    },
)
job = json.loads(body)
check("POST /api/jobs → 202 queued", status == 202 and job["status"] == "queued", job.get("id", ""))
job_id = job["id"]

# 5. poll to completion (the in-app worker thread processes the queue)
deadline = time.time() + 90
final = None
while time.time() < deadline:
    status, _, body = request(f"/api/jobs/{job_id}")
    final = json.loads(body)
    if final["status"] in ("completed", "failed"):
        break
    time.sleep(2)
check("job reaches completed", final["status"] == "completed", f"provider={final.get('provider')} err={final.get('error')}")
check(
    "job reused the preview download",
    final.get("notes") and "preview" in final["notes"].lower(),
    final.get("notes", ""),
)
check(
    "job result metadata",
    final["clip_url"] == f"/api/jobs/{job_id}/clip"
    and final["output_size_bytes"] > 1000
    and 4.5 <= (final["output_duration_seconds"] or 0) <= 5.5,
    f"{final['output_duration_seconds']}s / {final['output_size_bytes']} bytes",
)

# 5b. the clip must be 9:16 (1080x1920) — probe the actual bytes
import subprocess
import tempfile

status, _, body = request(final["clip_url"])
with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
    fh.write(body)
    tmp_name = fh.name
probe = subprocess.run(
    ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", tmp_name],
    capture_output=True, text=True, timeout=60,
)
dims = probe.stdout.strip()
check("clip is 9:16 (1080x1920)", dims == "1080,1920", dims)
import os

os.unlink(tmp_name)

# 6. fetch the clip (200) and a Range request (206) like a video element does
status, headers, body = request(final["clip_url"])
check("GET clip streams video/mp4", status == 200 and headers.get("Content-Type") == "video/mp4" and len(body) > 1000)
status, headers, body = request(final["clip_url"], headers={"Range": "bytes=0-1023"})
check("Range request → 206 partial", status == 206 and len(body) == 1024 and "Content-Range" in headers)

# 7. download disposition
status, headers, body = request(final["download_url"])
check(
    "download=1 → attachment",
    status == 200 and "attachment" in headers.get("Content-Disposition", ""),
)

# 8. history shows the record
status, _, body = request("/api/jobs?limit=10&offset=0")
history = json.loads(body)
check("history lists the job", history["total"] >= 1 and any(j["id"] == job_id for j in history["items"]))

# 9. validation rejection through the gateway
status, _, body = request("/api/jobs", method="POST", body={"url": "https://vimeo.com/x", "start_time": "1", "end_time": "2"})
payload = json.loads(body)
check("invalid URL rejected with 422", status == 422 and payload["error"]["code"] == "validation_error")

# 10. bad preview URL rejected
status, _, body = request("/api/previews", method="POST", body={"url": "https://vimeo.com/x"})
payload = json.loads(body)
check("invalid preview URL rejected with 422", status == 422 and payload["error"]["field"] == "url")

print("\nE2E OK — all checks passed.")
