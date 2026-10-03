# FRONTEND.md — Handoff Guide for the Front-End Developer

> **Who this is for:** you are taking over (or extending) the **frontend** of
> the YouTube Clipper web app. This document gives you the full context: what
> the app is, how to run it, the exact API contract, how the current frontend
> is built, and what you must not break.
>
> **Current state:** Phases 1–3.5 are complete and browser-verified. The
> frontend is a working, polished, dark video-editor UI. You are inheriting a
> working product, not a blank page — read §7 (flows that must keep working)
> before rewriting anything.

---

## 1. What this app is (30 seconds)

Paste a YouTube URL → the video starts playing almost immediately (instant
stream + background caching) → select a range on a scrollable filmstrip
timeline → get a **9:16 (1080×1920) Reels/Shorts-ready clip** with the source
centered on a blurred backdrop. Personal-use tool, free-tier hosting
(Render + Neon), clips auto-deleted after 24 h (metadata kept).

Backend: **FastAPI** (Python) — jobs, previews, rate limiting, retention.
Frontend: **vanilla HTML/CSS/JS, zero dependencies, no build step** — served
by the backend itself.

## 2. Repo map

```
├─ frontend/                  ← YOU LIVE HERE
│  ├─ index.html              page structure (5 sections, see §6)
│  ├─ app.js                  all app logic (~870 lines, function map in §6)
│  ├─ timeline.js             standalone Timeline component (no deps, §6.3)
│  └─ styles.css              dark editor theme (design tokens at top)
├─ backend/                   FastAPI app (app/), tests (tests/), alembic/
├─ docs/
│  ├─ openapi.json            ← FULL machine-readable API contract (12 paths)
│  ├─ deployment-guide.md     Render + Neon click-by-click
│  ├─ feasibility-report.md   why this hosting plan
│  ├─ phase-1/2/3-report.md   what was built when
│  └─ ai-integration.md       Phase-4 AI extension plan (stub exists)
├─ scripts/                   sandbox runner + e2e checks (§10)
├─ Dockerfile                 production image (ffmpeg + yt-dlp + fonts)
├─ render.yaml                Render blueprint (free plan)
└─ .env.example               every env var, documented
```

## 3. Architecture — how the frontend is served

- The FastAPI app mounts the `frontend/` directory at `/` with
  `StaticFiles(html=True)` **last**, after all `/api/*` routes. One origin,
  one URL, zero CORS in the default deployment.
- All API calls go through relative URLs (`/api/...`) — see `apiUrl()` in
  app.js.
- **Split deployment (optional):** if the frontend moves to another host
  (e.g. Vercel):
  1. set `window.CLIPPER_API_BASE = "https://<backend>.onrender.com"` in
     index.html (top of `<head>`, documented inline), and
  2. set `CLIPPER_ALLOWED_ORIGINS=<frontend origin>` on the backend.
  The backend then emits full CORS headers (GET/POST/OPTIONS, Range
  forwarding). Nothing else changes.

### The sandbox quirk (only if you develop inside the Z.ai build sandbox)

When the page is served through the sandbox's Caddy gateway
(`?XTransformPort=8000` in the URL), asset URLs must repeat that query
parameter. index.html contains a small bootstrap `<script>` that rewrites
`styles.css` / `timeline.js` / `app.js` URLs accordingly, and `apiUrl()`
propagates the parameter to API calls. **In production (Render) the parameter
is absent and plain relative URLs are emitted.** Do not remove this bootstrap;
it is inert outside the sandbox.

## 4. Run the stack locally (5 minutes)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

# minimal dev config — the "sample" provider synthesizes a test video,
# so the FULL UI flow works with zero external dependencies:
cat > backend/.env <<'EOF'
CLIPPER_DOWNLOADER_PROVIDERS=sample
EOF

cd backend && uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
```

- No database config needed — SQLite lands in `backend/data/` automatically
  (gitignored). Migrations run at boot.
- `CLIPPER_DOWNLOADER_PROVIDERS=sample` is the frontend developer's best
  friend: paste ANY youtube.com URL and the backend synthesizes a local test
  video, so preview → timeline → clip → download all work offline.
  Real providers (`cobalt,ytdlp`) need credentials (see `.env.example`).
- Health check: `GET /api/health` → `{"status":"ok","checks":{...}}`.
- Frontend dev = plain file editing; the page has no build/transpile step.
  Hard-refresh to pick up changes (`--reload` only restarts the backend,
  static files are served straight from disk).

## 5. The API contract (what you build against)

Full spec: **`docs/openapi.json`** (FastAPI-generated, always current).
Summary of everything the frontend touches:

| Method & path | Purpose | Notes |
|---|---|---|
| `POST /api/previews` `{url}` | Start instant-load pipeline | **202**. Dedupes: same URL → returns the existing in-flight or READY preview instead of re-downloading |
| `GET /api/previews/{id}` | Poll preview state | The poll payload — see lifecycle below |
| `GET /api/previews/{id}/stream` | **The** playback URL | Proxies the provider stream (Range forwarded → seeking works) until the local file lands, then serves the file. One URL for the preview's whole life |
| `GET /api/previews/{id}/thumbs/{name}.jpg` | Filmstrip tiles | READY previews only; URLs listed in the poll payload (`thumbs[]`) |
| `GET /api/previews/{id}/video` | Legacy alias for the local file | Prefer `/stream` |
| `GET /api/styles` | Style registry | UI renders its controls from this (§6) |
| `POST /api/jobs` | Create a clip | **202**. Body: `{url, start_time, end_time, style_id, style_params?, preview_id?}` |
| `GET /api/jobs/{id}` | Poll job | Until `completed` / `failed` |
| `GET /api/jobs?limit&offset&status` | History | Paginated |
| `GET /api/jobs/{id}/clip` | Stream the finished clip | `?download=1` → attachment. Range supported |
| `DELETE /api/jobs/{id}` | Delete clip file now | Record kept |
| `GET /api/health`, `GET /api/meta` | Health / limits+notice | `meta.limits` drives UI hints; `meta.notice` is the ToS text (display it) |

### 5.1 Preview lifecycle (the instant-load state machine)

```
pending → resolving → streaming ─┐
                   └→ downloading ┴→ processing → ready
any step → failed        ready → expired (after retention window)
```

- `status` values: `pending, resolving, streaming, downloading, processing,
  ready, failed, expired`.
- **The key trick:** `duration` and `video_url` appear in the poll payload
  **as soon as the provider resolves the stream** — while status is still
  `streaming`/`downloading`. Build the editor (player + timeline) the moment
  both exist; don't wait for `ready`.
- `progress` (0..1) = background cache download; becomes `1.0` at `ready`.
- `thumbs[]` is only populated at `ready` (60 tiles by default).
- Poll payload fields: `id, url, video_id, status, error, provider,
  stream_provider, title, duration, width, height, thumb_count, thumbs,
  video_url, progress, expires_at, created_at, updated_at`.

### 5.2 Job lifecycle

`queued → downloading (only without preview_id) → clipping → completed |
failed`

- With `preview_id`, the job runs straight from the cached file (no second
  download; `notes` says so). **In-flight previews are accepted** — the job
  waits in `queued` until the preview is READY, then runs. If the preview
  fails/expires, waiting jobs fail with a clear error (never hang).
- `preview_id` must belong to the same URL as `url` (422 otherwise).
- Times: `start_time`/`end_time` accept seconds (`"90"` / `90`) **or**
  timecodes (`"1:30"`, `"1:02:03"`).
- Job payload fields: `id, source_url, video_id, video_title,
  start_seconds, end_seconds, start_timecode, end_timecode,
  duration_seconds, style_id, style_params, status, error, provider, notes,
  preview_id, output_size_bytes, output_duration_seconds, clip_url,
  download_url, file_deleted_at, created_at, updated_at`.
- `clip_url`/`download_url` are only non-null when `completed`.
- Expired files: clip endpoints return **410 Gone** with a human-readable
  message ("resubmit the same range").

### 5.3 Errors & rate limits

- Every error is JSON: `{"error": {"code", "message", "field"?, "details": []}}`
  with a matching HTTP status (422 validation, 404, 409 conflict,
  410 gone, 413, 429).
- Rate limits (per client IP): 60 general req/min, 6 job creations/min,
  6 preview creations/min. **Media GETs (clip, preview video/stream/thumbs)
  are exempt** — a filmstrip + video element legitimately fires dozens.
- On 429/5xx: back off and retry; the current frontend's pollers do exactly
  that.

### 5.4 Styles endpoint shape

```json
[{
  "id": "original",
  "name": "Original",
  "description": "…",
  "parameters": [
    {"name": "background", "type": "enum", "default": "blur",
     "description": "…", "choices": ["blur", "black"]}
  ]
}]
```

Parameter `type` is currently `"enum"` (render a select) or `"bool"`.
`style_params` in POST /api/jobs = `{ "<parameter name>": <value> }`.
**The UI must render style controls dynamically from this response** — new
styles/params appear on the backend with zero frontend changes.

---

## 6. The current frontend, file by file

### 6.1 `index.html` (~230 lines)

Five top-level sections inside `<main>`, in order:

1. **`#load-card`** — the URL form (`#load-form`, input `#url`).
2. **`#editor`** (hidden until a preview loads) — editor grid:
   left = 16:9 player (`#player`, `#player-preparing` overlay, transport),
   right = "phone frame" canvas (`#reels-canvas` 288×512) showing a live
   9:16 output preview; below = the timeline block (toolbar + ruler canvas +
   `#tl-viewport` + hint line). `#cache-pill` shows background-cache state.
3. **`#clip-card`** — the clip form: start/end inputs (kept in sync with the
   timeline), `#style` select, `#style-params` (dynamic controls),
   `#limits` hint.
4. **`#status-section`** — job progress, `#error-box`, result grid
   (`#preview` video in a phone frame + `#download-btn`).
5. **History card** — `#history-table`.

Plus: ToS `<details class="notice">` at the top and a footer retention note
(**both must stay** — see §8), `window.CLIPPER_API_BASE` config and the
sandbox bootstrap (§3).

### 6.2 `app.js` (~870 lines, no framework)

Function map (top→bottom, grouped by responsibility):

- **API plumbing:** `apiUrl(path)` (API base + sandbox param),
  `request(path, options)` (fetch wrapper with retry-friendly errors).
- **Formatting:** `fmtShort`, `fmtTimecode`, `fmtBytes`, `fmtDate`, `el`.
- **Preview flow:** `loadPreview(url)` → `pollPreview(id)` (progressive
  poller; builds the editor as soon as `duration`+`video_url` exist) →
  `applyPreview` / `updateEditorProgress` (cache pill + timeline progress) →
  `finishPreview` (thumbs). `showManualFallbackNote` — graceful manual-mode
  message when the preview pipeline fails but jobs still work.
- **Player:** `updatePlayerSource` (single `/stream` URL),
  `updateVideoSub`, `updateTransport`, `togglePlay`, `startFrameLoop`,
  `drawReelsFrame` (composites the 9:16 output preview on canvas),
  `startReelsPreview`.
- **Timeline wiring:** `buildTimeline(preview)`, `onSelectionChange`
  (→ clip form fields), `onScrub` (→ player currentTime).
- **Style controls:** `loadStyles`, `renderStyleParams` (dynamic from
  `/api/styles`), `collectStyleParams`.
- **Job flow:** `submitJob` (sends `preview_id` whenever the preview is
  usable — that's the cache-hit fast path), `poll(jobId)`, `renderStatus`,
  `renderResult` (result video + meta + download).
- **History:** `loadHistory` (+ refresh button), delete handling.
- **Boot:** `loadMeta` (limits + provider chip), `resetEditor`
  (change video), `bindEvents` (all listeners incl. keyboard: I/O/space).

### 6.3 `timeline.js` — standalone filmstrip component (no dependencies)

Exposed as `window.Timeline`. Constructor options include `onChange(start,
end)` (selection moved) and `onSeek(t)` (ruler scrub). Public methods:

| Method | Purpose |
|---|---|
| `setThumbs(urls)` | filmstrip images (call again on READY — upgrades in place) |
| `setProgress(fraction)` | hatched background-cache fill (0..1) |
| `clearProgress()` | remove the fill |
| `destroy()` | detach listeners |

Built-in interactions: dual drag handles + time bubbles, move/drag selection,
drag-empty-to-select, canvas time ruler scrub, wheel pan, Ctrl+wheel zoom,
keyboard nudging, Fit button. It is intentionally framework-agnostic — reuse
it as-is in any rewrite.

### 6.4 `styles.css`

Dark video-editor theme; design tokens (colors, radii, spacing) are CSS
custom properties at the top. Layout is CSS grid; note the `min-width: 0`
fixes on grid children — they keep zoomed timelines scrolling inside the
card instead of widening it. Responsive down to mobile.

## 7. UX flows that must keep working (acceptance criteria)

1. **Instant load:** paste URL → editor (player + timeline) renders within
   ~1 s from `video_url`+`duration` arriving, while the cache pill shows
   live `progress`; timeline shows a hatched fill until cached.
2. **Selection:** drag handles / drag-empty-to-select / I+O keys / ruler
   scrub / zoom / pan; start+end fields stay in sync with the handles.
3. **Submit:** job goes straight to `clipping` when reusing the cached
   preview (no re-download); waits correctly if the preview is still caching.
4. **Result:** 1080×1920 video plays in the phone frame; Download works
   (attachment filename with title + range).
5. **History:** refresh, statuses, expired clips show a 410-driven message.
6. **Failure paths:** preview failure → manual fallback note (jobs without
   preview still work); 429s retried; expired preview → clear guidance to
   reload.
7. **Zero console errors** through the whole journey (this was verified at
   every phase — keep it that way).

## 8. Rules — do & don't

**Don't:**
- Don't change API paths/shapes without updating `docs/openapi.json` and the
  backend together — the contract is the source of truth.
- Don't remove or hide the **Terms & usage notice** or the retention footer
  (they're a deliberate product decision, echoed from `/api/meta`).
- Don't add a build step, bundler, or framework **while staying in the
  single-service deployment** — the backend serves raw files from `frontend/`
  and there is nothing to run a build (that's the point of vanilla JS here).
- Don't poll media endpoints through JS state machines — the `<video>`
  element itself does Range requests; just set `src`.
- Don't break the `preview_id` fast path — always send it when usable.

**Do:**
- Keep everything dependency-free unless you take the split-deploy path (§9).
- Keep controls registry-driven from `/api/styles`.
- Reuse `timeline.js` as-is if you rewrite anything else.
- Test with the `sample` provider (§4) — it exercises the entire flow.
- Read `docs/openapi.json` when in doubt about a field.

## 9. If you want React/Vue/anything with a build step

That's the **split deployment**: frontend on Vercel (or any static host with
a build pipeline), backend stays on Render.

1. Deploy backend as today (Render blueprint, `render.yaml`).
2. New frontend project → build output served by Vercel.
3. Point it at the backend: `CLIPPER_API_BASE` equivalent (env-driven in
   your framework of choice) = `https://<backend>.onrender.com`.
4. Backend env: `CLIPPER_ALLOWED_ORIGINS=https://<frontend>.vercel.app`.
5. Port the flows in §7 — the acceptance criteria are framework-agnostic.

CORS specifics already handled by the backend: GET/POST/OPTIONS, Range
forwarding for cross-origin video seeking, exposed `Content-Range` headers.

## 10. Testing & verification

- **Backend unit tests:** `cd backend && python -m pytest` — 180 tests
  (validation, lifecycle, persistence, clipping accuracy, previews, CORS,
  downloader matrix). They run offline; no credentials needed.
- **E2E through the sandbox gateway:** `scripts/e2e_gateway_check.py` — 30
  checks covering the full user journey (boot it with
  `scripts/sandbox_backend.sh` first; both must run in the same shell
  session inside the sandbox).
- **Manual checklist:** §7 verbatim.
- After any frontend change: hard-refresh, run one full
  paste→select→clip→download journey with the `sample` provider, watch the
  console.

## 11. Known limitations & suggested next steps

Current limitations (by design, v1):
- "Original" style only (9:16 centered + blur/black backdrop).
- 10-minute max clip, 4-hour max source (server-enforced; surfaced via
  `/api/meta`).
- No auth — rate limiting is the only abuse protection (personal use).
- AI suggestion endpoint is a stub (`POST /api/ai/suggest` → 503 unless
  enabled); plan in `docs/ai-integration.md`.

Ideas (roughly in value order):
1. Mobile UX polish (timeline gestures are desktop-first right now).
2. Shareable clip links (needs a public-file decision — retention is 24 h).
3. AI highlight suggestions (backend extension point ready).
4. More styles (backend: one file in `backend/app/styles/`; UI picks it up
   automatically).
5. Upload own file instead of YouTube URL (larger change, touches
   validation + providers).

## 12. Where to find more

- `README.md` — project overview + API table.
- `docs/openapi.json` — the API contract, always current.
- `docs/deployment-guide.md` — hosting (Render + Neon) click-by-click.
- `docs/phase-*-report.md` — what each phase delivered and why.
- `docs/ai-integration.md` — the Phase-4 AI plan.
- `.env.example` — every backend knob, documented.

