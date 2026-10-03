# YouTube Clipper

Paste a YouTube URL, scrub a **scrollable filmstrip timeline** to select the
portion you want, and get a **9:16 Reels/Shorts clip (1080×1920)** with the
video centered. A personal-scale tool designed for **free-tier hosting**
(Render web service + Neon Postgres), with pluggable download providers,
pluggable clip styles, persistent history, and a documented extension point
for future AI-assisted clipping.

> **Legal / Terms-of-Service notice.** Downloading YouTube content through
> third-party services may violate YouTube's Terms of Service and may
> infringe copyright depending on the video and your use. You are
> responsible for having the rights to clip and use the content. This
> project includes no features intended to hide or circumvent this notice.
> (Also shown in the app UI and `GET /api/meta`.)

## Repository layout

```
backend/            FastAPI application (Phase 1)
  app/
    api/            REST routes (jobs, previews, styles, health/meta, AI stub)
    core/           validation, ffmpeg runner, rate limiting, errors
    db/             SQLAlchemy models, engine factory, repository
    downloader/     pluggable providers: cobalt | ytdlp | sample
    styles/         pluggable clip styles (auto-discovered)
    services/       orchestrator, preview pipeline, workers, retention sweeper
    ai/             Phase 4 extension point (protocol + stub, no logic)
  alembic/          migrations (0001 = jobs, 0002 = previews + jobs.preview_id)
  tests/            148 offline tests (providers mocked/synthetic)
frontend/           Plain HTML/JS editor UI served by the backend (Phases 2–3)
docs/               feasibility report, phase reports, OpenAPI, AI guide
Dockerfile          production image (ffmpeg + fonts + Python deps)
render.yaml         Render blueprint (free plan)
.env.example        every environment variable, documented
```

*(The `src/`, `public/`, `package.json` etc. at the repo root belong to the
build-sandbox preview shell — a Next.js page that embeds the real app for
preview purposes only. They are not part of the deployable application.)*

## Local setup

Requirements: Python 3.12+, ffmpeg + ffprobe on PATH.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env       # then edit (see below)
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 — the UI and API are served from one origin.
Interactive API docs: http://localhost:8000/docs (Swagger UI).

## Environment variables

See [`.env.example`](.env.example) for the complete list with descriptions.
The essentials:

| Variable | Purpose | Default |
|---|---|---|
| `CLIPPER_DATABASE_URL` | Postgres in production (Neon); empty = local SQLite | SQLite file |
| `CLIPPER_DOWNLOADER_PROVIDERS` | Ordered provider chain | `cobalt,ytdlp` |
| `CLIPPER_COBALT_API_URL` / `_KEY` | Your cobalt-compatible instance | empty |
| `CLIPPER_YTDLP_COOKIES_FILE` | cookies.txt for datacenter-IP bot checks | empty |
| `CLIPPER_MAX_CLIP_SECONDS` | Max clip length | `600` |
| `CLIPPER_MAX_SOURCE_SECONDS` | Max source length | `14400` |
| `CLIPPER_CLIP_RETENTION_HOURS` | Clip file retention (metadata kept forever) | `24` |
| `CLIPPER_PREVIEW_THUMB_COUNT` | Filmstrip tiles per preview | `60` |
| `CLIPPER_PREVIEW_RETENTION_HOURS` | Preview file retention | `24` |

All variables use the `CLIPPER_` prefix so they can't collide with
platform-injected variables (e.g. Render's own `DATABASE_URL`).

## Running the tests

```bash
cd backend
python -m pytest tests/
```

**148 tests**, fully offline: the downloader is exercised through the local
`sample` provider (ffmpeg-synthesized video) and mocked HTTP for the cobalt
client — no third-party site is ever contacted (spec §3 Phase 1).

## API summary

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/jobs` | Create clip job (202) — `{url, start_time, end_time, style_id, style_params?, preview_id?}`; times accept seconds or `HH:MM:SS` |
| GET | `/api/jobs` | History with pagination (`limit`, `offset`, optional `status`) |
| GET | `/api/jobs/{id}` | Job status/result (incl. `clip_url`, `download_url`, timecodes) |
| GET | `/api/jobs/{id}/clip` | Stream the clip (Range/206 supported); `?download=1` for attachment |
| DELETE | `/api/jobs/{id}` | Delete the clip file now; keep the record |
| POST | `/api/previews` | Queue a timeline preview (202) — instant stream resolve + background full download; same-URL requests dedupe to the ready **or in-flight** preview |
| GET | `/api/previews/{id}` | Preview status/metadata — `duration`, `video_url` and `progress` appear while still `streaming`/`downloading`, `thumbs[]` when ready |
| GET | `/api/previews/{id}/stream` | **The** playback URL: proxies the provider's direct media URL (Range forwarded) until the local file lands, then serves the file |
| GET | `/api/previews/{id}/video` | Legacy alias for the downloaded file (READY previews only, Range/206) |
| GET | `/api/previews/{id}/thumbs/{n}.jpg` | Filmstrip tile (JPEG) |
| GET | `/api/styles` | Clip styles (id, name, description, parameters) |
| GET | `/api/health` · `/api/meta` | Liveness/limits/notices |
| POST | `/api/ai/suggest` | **501 stub** — Phase 4 extension point |

Media endpoints (clip / preview video / stream / thumbnails) are exempt from
the per-IP request counter: a `<video>` element legitimately issues dozens of
Range requests and a filmstrip loads ~60 tiles at once. The action endpoints
(`POST` jobs/previews) keep their own strict buckets.

Full machine-readable spec: [`docs/openapi.json`](docs/openapi.json), or live
at `/openapi.json` + Swagger UI at `/docs` when the server runs.

Error shape (all endpoints): `{"error": {"code", "message", "field?", "details?"}}`.

## Deployment (free tier)

Full analysis in [`docs/feasibility-report.md`](docs/feasibility-report.md).
**Step-by-step instructions (every click, no credit card):
[`docs/deployment-guide.md`](docs/deployment-guide.md).**
Recommended: **Render free web service + Neon free Postgres** — one Docker
service serves the API *and* the frontend at the same URL. A split setup
(frontend on Vercel, backend on Render) is also supported: set
`window.CLIPPER_API_BASE` in `frontend/index.html` and
`CLIPPER_ALLOWED_ORIGINS` on the backend.

### 1. Neon (database, no card required)

1. Create a free project at neon.tech → copy the connection string
   (`postgresql://…?sslmode=require`).

### 2. Render (backend + UI, one service)

1. Push this repository to GitHub (done:
   `github.com/Aryan-verma-star/ytclipping`).
2. Render → New → Blueprint → select the repo (uses `render.yaml`).
3. When prompted, set:
   - `CLIPPER_DATABASE_URL` = your Neon string
   - `CLIPPER_COBALT_API_URL` (+ `CLIPPER_COBALT_API_KEY`) if you have an
     instance, and/or `CLIPPER_YTDLP_COOKIES` — your cookies.txt content
     pasted as an env var (or `CLIPPER_YTDLP_COOKIES_FILE` as a path)
4. Deploy. The Docker image installs ffmpeg; schema migrations run at boot.

Free-tier behavior to expect (all handled by the app):

- The service **spins down after ~15 min idle** — first request is slow.
- **Disk is ephemeral**: clip files vanish on redeploy/restart. Metadata
  survives in Neon; expired/missing files return `410` with a clear message.
- A job interrupted by a restart is marked `failed` ("service restarted…")
  at the next boot — resubmit it.
- **YouTube bot-checks datacenter IPs** (verified 2026-10-03): from Render,
  the `ytdlp` provider needs `CLIPPER_YTDLP_COOKIES_FILE`, or use a working
  cobalt instance. From your home machine it usually works with no cookies.

### Alternatives (documented, not recommended for this workload)

- **Vercel (Hobby)**: Fluid compute now allows long sessions, but the ~4
  CPU-hours/month budget is quickly consumed by ffmpeg re-encodes, and cron
  runs once/day — see feasibility report §1.2.
- **Cloudflare**: Workers free (~10 ms CPU/invocation) **cannot** run ffmpeg;
  Pages could host the static frontend only — §1.3.
- **Supabase** instead of Neon: free 500 MB Postgres + 1 GB file storage if
  you want clips to outlive redeploys.

## Adding a new clip style (one file, nothing else)

Create `backend/app/styles/my_style.py`:

```python
from app.styles.base import ClipStyle, StyleParameter, register_style

class VerticalStyle(ClipStyle):
    id = "vertical"
    name = "Vertical 9:16"
    description = "Center-crop for Shorts/Reels/TikTok."
    parameters = [StyleParameter("blur", type="boolean", default=False,
                                 description="Blurred background instead of crop")]

    def apply(self, source, output, *, start_in_source, duration, params):
        # run ffmpeg here; raise on failure
        ...

STYLE = VerticalStyle()          # auto-registered by package autodiscovery
```

That's it — the registry scans `app/styles/` at startup; the style appears
in `GET /api/styles` and in the UI dropdown (with its parameter controls)
automatically. No API, schema, or UI changes. (See
`tests/test_styles.py::test_adding_a_new_style_requires_only_registration`
for the executable proof.)

The shipped `original` style is itself the reference implementation: it
composes the 9:16 frame (1080×1920), scales the source to fit, centers it,
and fills the surrounding space per its `background` parameter — `blur`
(a zoomed, blurred copy of the video, the classic reels look; blurred at
1/5 resolution then upscaled because the backdrop is blurred anyway and
free-tier CPUs are tiny) or `black` (plain letterbox bars).

## Adding / swapping a downloader provider

1. Create `backend/app/downloader/my_provider.py` implementing
   `DownloaderProvider.get_video(url, start, end) -> VideoSource`
   (raise `ProviderError` — never crash — on any failure).
2. Add it to `PROVIDERS` in `backend/app/downloader/registry.py` (one line).
3. Put its name anywhere in the `CLIPPER_DOWNLOADER_PROVIDERS` chain.

Shapping providers is pure configuration; nothing outside the provider
module and its registry entry changes (spec §5).

**About third-party download services:** they are unofficial, change
frequently, and can break without notice — the `cobalt` provider is expected
to need maintenance over time. Failures always surface as a clear `failed`
job status with the provider's own message.

## The timeline editor (Phase 3) — instant load (Phase 3.5)

The UI is a dark, video-editor-style flow built around a real-time experience:

1. **Paste a URL — it plays immediately.** The backend asks the provider chain
   for a *stream target* (cobalt: the direct tunnel URL + a cheap remote
   ffprobe; yt-dlp: a muxed progressive format via metadata-only `-J`; sample:
   the known duration). The editor appears with the timeline and a playable
   video through `GET /api/previews/{id}/stream` — a Range-forwarding proxy —
   *while the full-quality file keeps downloading in the background*.
2. **Watch the cache fill.** A pill in the editor header (`Caching in
   background · 47%`) and a progress fill across the timeline track show the
   background download; filmstrip thumbnails pop in when the file lands.
   Providers without a resolvable stream (or the synthetic sample provider)
   skip the proxy phase but still render the timeline instantly from the
   resolved duration.
3. **Scrub and select** — scrollable filmstrip timeline with dual handles, a
   time ruler (click/drag to scrub), drag-the-middle to move the selection,
   zoom (buttons or Ctrl+scroll), pan (scroll), `I` / `O` / `space`
   shortcuts. Start/end inputs stay in sync for precise typing.
4. **See the output while editing** — a live 9:16 "phone" preview (canvas)
   renders exactly what the clip will look like: video centered on the
   blurred backdrop.
5. **Create the clip any time** — jobs accept a `preview_id` even while the
   preview is still downloading; the worker holds them until the cache is
   ready, then cuts from it. The paste → scrub → clip flow downloads the
   source exactly once, and a preview that fails/expires takes its waiting
   jobs down with a clear error instead of hanging the queue.

If the preview cannot be prepared (e.g. datacenter-IP bot checks without
cookies), the UI degrades gracefully to manual time entry.

## Where the future AI module plugs in (Phase 4)

See [`docs/ai-integration.md`](docs/ai-integration.md). Short version:
implement the `VideoAnalyzer` protocol in `backend/app/ai/`, register it,
and suggestions feed straight into `POST /api/jobs`. The stub endpoint
`POST /api/ai/suggest` documents the seam (returns 501 today).

## Status

- **Phase 1 (backend)** — complete, tests passing (163 offline).
- **Phase 2 (minimal test UI)** — complete, browser-verified end to end.
- **Phase 3 (timeline editor + 9:16 reels output)** — complete: preview
  pipeline with filmstrip, scrollable dual-handle timeline, live 9:16
  output preview, dark editor theme, responsive layout; browser-verified
  (23/23 gateway checks incl. preview flow, 1080×1920 output probe,
  zero console errors).
- **Phase 3.5 (instant load + background download)** — complete: provider
  `resolve_stream()` capability (cobalt/yt-dlp/sample), `/stream` Range
  proxy, live progress reporting, jobs queued against in-flight previews,
  waiting-job failure cascade, dedupe of in-flight previews; verified with
  163/163 offline tests, 29/29 gateway checks and a real-browser run
  (editor live ~1 s after paste, caching pill + timeline fill observed,
  zero console errors).
- **Phase 4 (AI)** — extension point only, per spec.
