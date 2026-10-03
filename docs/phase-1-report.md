# Phase 1 Report — Backend

**Status: COMPLETE.** All items from spec §3 Phase 1 delivered and tested.

## What was built

- **REST API** (FastAPI, `backend/`):
  - `POST /api/jobs` — create a clip job (url, start_time, end_time, style_id, style_params). Returns 202 + job.
  - `GET /api/jobs/{id}` — status & result (timecodes, clip_url, download_url, error, provider, notes).
  - `GET /api/jobs` — history with `limit`/`offset` pagination + optional `status` filter.
  - `GET /api/jobs/{id}/clip` — stream (Range/206 supported) or `?download=1` attachment.
  - `DELETE /api/jobs/{id}` — delete clip file, keep the record.
  - `GET /api/styles` — dynamic style listing (id, name, description, parameters).
  - `GET /api/health`, `GET /api/meta` — liveness, limits, ToS notice.
  - `POST /api/ai/suggest` — 501 stub (Phase 4 extension point).
- **Input validation**: watch/youtu.be/shorts/embed/live URL formats; start<end; clip-length cap (600 s default); source-length cap (14 400 s default); actual-video-duration enforcement during processing (fail if start is beyond the end; clamp + note if end is past it).
- **Pluggable downloader providers** behind `DownloaderProvider.get_video(url, start, end)`:
  - `cobalt` — third-party download API (cobalt-compatible schema), instance URL + optional bearer key from env, tunnel/redirect download with byte cap; nothing hardcoded.
  - `ytdlp` — locally-run yt-dlp fallback: `--download-sections` segment-only downloads, `--force-keyframes-at-cuts`, height cap, optional cookies file, friendly error mapping.
  - `sample` — clearly-labeled dev/test provider (ffmpeg-synthesized video with burned-in timecode).
  - Ordered fallback chain via `CLIPPER_DOWNLOADER_PROVIDERS`; every provider failure is captured per-provider and reported on the job.
- **Pluggable style registry**: auto-discovered modules in `app/styles/`; v1 ships `original` (re-encode trim, frame-accurate, with a `fast_copy` parameter). Adding a style = one new file.
- **Persistence**: SQLAlchemy 2.0 model covering every spec §7 field (plus `notes`, `provider`, `output_duration_seconds`); Alembic migration `0001` runs automatically at boot; SQLite (dev/test) ↔ Postgres/Neon (prod) switch via `CLIPPER_DATABASE_URL`.
- **Job orchestration**: single in-process worker thread (free-tier reality), status flow queued→downloading→clipping→completed|failed, startup recovery of stale jobs, temp-file cleanup in `finally`.
- **Retention sweeper**: deletes clip files older than the window (default 24 h), sets `file_deleted_at`, keeps metadata forever; `GET .../clip` then returns 410 with a resubmit hint.
- **Abuse protection**: per-IP sliding-window rate limits (separate bucket for job creation) + request-size cap (64 KB).
- **AI extension point**: `VideoAnalyzer` protocol + registry in `app/ai/analyzer.py`, 501 stub route, `docs/ai-integration.md`.

## How it was tested (all actually run, 2026-10-03)

`python -m pytest tests/` → **129 passed**, fully offline:

- `test_validation.py` (35 cases) — URL formats accepted/rejected, time parsing, window rules.
- `test_api_jobs.py` (24) — creation happy path + 8 invalid-input classes, full lifecycle via the sample provider, failure path with an injected failing provider, history pagination/ordering/filtering, Range/206, 410 after deletion, rate-limit 429, payload 413, AI stub 501, health/meta/styles.
- `test_persistence.py` (5) — records survive app restart, retention keeps metadata after file deletion, stale-job recovery, schema created by Alembic, real worker thread processes the queue end to end.
- `test_clipping.py` (4) — re-encode trim duration accuracy (±0.3 s), stream-copy behavior, ffmpeg error propagation.
- `test_downloader.py` (17) — sample provider real synthesis, yt-dlp command construction, cobalt behavior matrix (tunnel/error/picker/401/429/non-JSON/unconfigured/transient flag) via mocked HTTP, chain fallback + all-failed aggregation.
- `test_styles.py` (7) — registry contents, executable extensibility proof (register a dummy style → it validates and lists), duplicate-id rejection, Original trim accuracy both modes.

## Broken or limited (honest list)

1. **Real-YouTube downloads do not work from datacenter IPs** — verified live: YouTube bot-checks yt-dlp from this sandbox (and the same will happen on Render); the official cobalt API rejects anonymous requests (`error.api.auth.jwt.missing`). Mitigations shipped: cookies-file support, env-configured cobalt instance, clear provider errors. Working end-to-end in this environment is proven via the `sample` provider.
2. **In-process worker**: jobs die if the service restarts mid-job (recovered as `failed` with a resubmit message). No free always-on worker exists on Render Free.
3. **Clip files are ephemeral** on Render Free — accepted tradeoff (metadata persists in Neon; documented).
4. **Rate limiting is in-memory per instance** — fine for a single free-tier instance, not for multi-instance scaling.
5. `max_download_bytes` cap (1.5 GB) rejects very long/hi-res sources on the cobalt path with a clear error rather than silently filling the disk.

## Needed from the user

- Your cobalt instance URL (+ key) or another third-party download service to wire in, **or** a cookies.txt for yt-dlp if running on a datacenter host.
- Nothing else is blocking Phase 2.
