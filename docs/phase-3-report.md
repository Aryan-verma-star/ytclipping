# Phase 3 Report — Timeline Editor + 9:16 Reels Output

**Date:** 2026-10-03 · **Scope of this phase:** user-requested Phase 3 features
(scrollable professional timeline, 9:16 output with centered video) plus the
backend preview pipeline that powers the timeline.

## What was built

### 1. The `original` style now produces 9:16 Reels/Shorts clips

- Output is always **1080×1920**; the source is scaled to fit and **centered**
  in the vertical frame (user decision).
- New registry-driven parameter `background`:
  - `blur` (default) — zoomed, blurred copy of the video fills the frame
    (classic reels look). Blurred at 1/5 resolution then upscaled: visually
    identical for a backdrop, ~15–20 % cheaper CPU (measured), which matters
    on 0.1-CPU free tiers.
  - `black` — plain letterbox bars.
- The old `fast_copy` parameter is gone (stream copy cannot compose frames);
  legacy clients sending it get a clear 422.
- `run_ffmpeg` now passes `-nostdin` — **real production bug found during
  verification**: with an inherited open stdin (`docker run -i`, terminals,
  some supervisors) ffmpeg enters an interactive command prompt after encoding
  and the call never returns.

### 2. Preview pipeline (the timeline's data source)

New `previews` table (Alembic 0002, auto-migrated at boot) + worker thread:

- `POST /api/previews` → 202; downloads the **full** source via the normal
  provider chain, probes duration/dimensions, generates ~60 evenly spaced
  JPEG filmstrip tiles (per-tile seeks — cheap even on 4 h sources).
- `GET /api/previews/{id}` for polling; `/video` streams the source with
  Range/206; `/thumbs/{n}.jpg` serves tiles.
- Same-URL requests dedupe to an existing ready preview.
- Retention: preview files swept after `PREVIEW_RETENTION_HOURS` (row kept,
  endpoints then answer 410); stuck previews time out (30 min default);
  startup recovery fails interrupted previews like jobs.
- **Job reuse:** `POST /api/jobs` accepts `preview_id` — the worker clips
  directly from the preview's downloaded file, so paste → scrub → clip
  downloads the source exactly once (verified: provider call count == 1).
- New rate-limit bucket for preview creation (each is a full download).

### 3. Rate limiting fix for media (found by browser verification)

A 60-tile filmstrip + a `<video>` element's Range requests exhausted the
general per-IP request counter; the job poller then 429'd and orphaned a
healthy job. Media endpoints (clip, preview video, thumbnails) are now exempt
from the request counter — their abuse surface is bounded by retention and
download caps. Action endpoints keep strict buckets. Frontend pollers also
retry transient failures instead of giving up.

### 4. Frontend: dark video-editor UI

- URL card with auto-load (debounced) and explicit Load button; graceful
  fallback to manual time entry when a preview can't be prepared.
- **Scrollable filmstrip timeline** (`frontend/timeline.js`, no deps):
  dual drag handles with time bubbles, drag-middle-to-move, drag-empty-to-
  select, adaptive time ruler (canvas, redrawn per scroll), click/drag ruler
  to scrub, wheel pan, Ctrl+wheel zoom, `I`/`O`/`space` shortcuts, keyboard-
  accessible handles (arrows nudge), playhead with auto-scroll while playing.
- **Live 9:16 output preview**: a canvas "phone frame" renders the composed
  output frame-for-frame (blurred cover backdrop + centered video) while
  editing.
- 16:9 source player with custom transport; style dropdown renders parameter
  controls from the registry (the `background` enum appears automatically).
- Result card shows the 9:16 clip in a phone frame, real dimensions read from
  the video element, and the download button.
- CSS grid `min-width: 0` fix: without it a zoomed filmstrip widened the card
  instead of making the timeline scroll.

## Verification (all executed, not assumed)

- **148/148 offline pytest** (18 new preview tests incl. reuse-counting,
  expiry→410, stale recovery, media-exemption; style tests now assert
  1080×1920 and pixel-verify the centered band vs. black bars).
- **23/23 gateway E2E** incl. preview lifecycle, thumbs JPEG magic, video
  206, preview_id mismatch 422, job reuse note, and an ffprobe of the served
  clip bytes: `1080,1920`.
- **Real headless-browser run**: paste URL → auto-load → 60 tiles loaded →
  synthetic pointer-event handle drag (selEnd 15→24.1 s) → I/O keys → zoom
  (2337 px scroll range) → ruler scrub → submit → result `1080x1920` in the
  player, "Source was reused from the cached preview download" note visible;
  **zero console errors**.
- **Screenshots** (in `download/`): initial, timeline editor with selection,
  completed 9:16 result, mobile 390 px layout. VLM review of the screenshots
  confirms: professional dark editor aesthetic, correct 9:16 phone preview,
  no overlapping/broken layout, responsive mobile view.
- `bun run lint` clean.

## Environment / config additions

`CLIPPER_PREVIEW_THUMB_COUNT` (60), `CLIPPER_PREVIEW_THUMB_HEIGHT` (120),
`CLIPPER_PREVIEW_RETENTION_HOURS` (24), `CLIPPER_PREVIEW_MAX_PROCESSING_MINUTES`
(30), `CLIPPER_RATE_LIMIT_PREVIEWS_PER_MINUTE` (6) — all documented in
`.env.example`.

## Known limits (unchanged)

- Datacenter IPs still need cookies (yt-dlp) or a cobalt instance for real
  downloads — the sandbox demo uses the clearly-labeled `sample` provider.
- Preview + clip encoding on Render free will be slow for long clips; the
  UI is async throughout, so this is a latency issue, not a failure.
