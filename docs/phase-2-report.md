# Phase 2 Report — Basic Test UI

**Status: COMPLETE and verified end to end.** Built after Phase 1 per spec
§3; Phase 3 (polish) has **not** been started, as instructed.

## What was built

Plain HTML/JS/CSS frontend (`frontend/`) served by the FastAPI backend at
`/` (same origin, no build step):

- Form: YouTube URL, start/end time inputs (accept `90` or `1:30` forms),
  style dropdown **populated dynamically from `GET /api/styles`**.
- Job status indicator with badges (queued / downloading / clipping /
  completed / failed) and live polling (2 s).
- Video preview (`<video controls>`, Range-streamed) + download button on
  completion; clear error box on failure.
- History table: created time, video link, timecode range, style, status
  (with error tooltip), clip open/download actions, refresh button.
- ToS notice visible in the header area (spec §11) and retention note in
  the footer; limits line from `GET /api/meta`.

Sandbox-only plumbing (not part of the deployable app): a Next.js `/` page
embedding the real app through the Caddy gateway (`?XTransformPort=8000`),
and a head in `index.html` that makes asset URLs gateway-aware. In
production none of this exists — FastAPI serves the UI directly.

## How it was tested (actually run, 2026-10-03)

1. **Gateway E2E script** (`scripts/e2e_gateway_check.py`), 13/13 PASS —
   UI served, assets routed, styles/meta endpoints, job created (202),
   worker processed it to `completed` (5.0 s clip, 1.17 MB), clip streamed
   (200 `video/mp4`), Range → 206, download disposition, history, and
   invalid-URL → 422 with `validation_error`.
2. **Real headless-browser verification** (agent-browser, exactly the path
   the preview panel takes): opened the shell page, filled the form
   (URL `…watch?v=aBcDeFgHiJk`, 0:03→0:08, style `original`), clicked
   Create clip. Network log confirms: `POST /api/jobs` 202 → four polls →
   `GET /api/jobs/{id}/clip` **206 (Media)**; the video player, Download
   link, and the new history row rendered; **zero page errors**. Screenshot:
   `download/phase2-ui-verification.png`.
3. Full backend suite still green: **129 passed**.

## What is deliberately missing (Phase 3 scope)

Visual polish, dark mode, timeline/range slider, style thumbnails,
skeleton loaders, mobile-first responsive design. The current CSS is
intentionally minimal-but-clean per spec ("no visual polish is required").

## Broken or limited

- In the **build sandbox**, jobs run through the `sample` provider first
  (synthetic video with burned-in timecode) because YouTube blocks
  datacenter IPs and no cobalt instance is configured. The UI labels the
  provider on every result ("via sample"). Switch `CLIPPER_DOWNLOADER_PROVIDERS`
  to change this.
- The browser `wait --text` automation couldn't match the status badge
  text node (tooling quirk); completion was verified via the rendered
  video player, download link, history row, and 206 media request instead.
- History auto-refreshes only after job completion or manual refresh.

## Needed from the user

1. Confirmation that Phase 2 works as expected on your side (preview panel).
2. A cobalt instance URL / API key, or a yt-dlp cookies file, to test real
   YouTube downloads in your deployment.
3. Go-ahead to start **Phase 3** (polished UI: dark mode, dual-handle
   timeline slider, style picker with thumbnail support, responsive layout).
