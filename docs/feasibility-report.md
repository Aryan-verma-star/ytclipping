# Feasibility Report & Architecture Recommendation — YouTube Clipper Web App

**Date:** 2026-10-03
**Status:** Delivered before implementation, per spec §4 / §8.1
**Verifier notes:** Facts marked **[VERIFIED]** were tested first-hand from the build environment on 2026-10-03. Facts marked **[DOC]** come from the platform's own documentation. Facts marked **[REPORTED]** come from third-party pricing trackers and may lag.

## 0. Locked configuration decisions (from §14 clarification round)

| Decision | Choice |
|---|---|
| Backend | Python / FastAPI |
| Frontend | Plain HTML/JS served by the backend (no build step) |
| Downloader | Hybrid: third-party download API (cobalt-compatible) primary, yt-dlp fallback |
| v1 clip styles | "Original" only; registry ships regardless |
| Max clip length | 600 s (env-configurable) |
| Usage profile | Personal; basic per-IP rate limiting |
| Database | External free-tier Postgres (Neon); SQLite for local dev/tests |
| Max source length | 4 h (env-configurable) |
| Clip file retention | 24 h (env-configurable); metadata kept forever |

These match spec Assumptions A–E where the user did not override.

## 1. The five §4 concerns, per candidate platform

### 1.1 Render (free web service)

| Concern | Finding |
|---|---|
| ffmpeg runtime | **[DOC]** Free web services: 512 MB RAM, shared CPU. **[VERIFIED]** No documented hard request timeout for web services; the app avoids the question entirely by processing jobs asynchronously (POST returns 202 immediately; client polls). A Docker-based deploy can bake in ffmpeg + yt-dlp. A 10-min 720p `veryfast` re-encode takes on the order of 1–4 min of CPU — well within a persistent web service's life. |
| Filesystem | **[DOC]** Ephemeral: local changes are lost on **redeploy, restart, AND spin-down**. Free tier cannot attach persistent disks. → Clips are temporary by nature here; metadata MUST live in an external DB. |
| Cold start / spin-down | **[DOC]** Free services spin down after ~15 min of inactivity; wake-up adds tens of seconds. 750 free instance-hours/workspace/month (a spun-down service consumes none). A job running at spin-down moment is killed → the app needs stale-job recovery on startup. |
| Response size limits | **[DOC]** No documented response-body cap for web services; video files stream directly from the service. Bandwidth counts against the workspace's monthly outbound allowance (100 GB/mo free tier). |
| Outbound networking | **[DOC]** Service-initiated outbound traffic (external DB, APIs) is allowed. **[VERIFIED]** But YouTube bot-checks datacenter IPs (see §1.4). |
| Card requirement | **[REPORTED]** Render may ask for a card in some signup flows (free-infrastructure abuse prevention). The free tier itself remains $0. If your flow demands a card and that's unacceptable, the closest free alternative for the backend is Koyeb's free instance or running the app locally; the architecture is unchanged (it's a plain Dockerized web service). |

### 1.2 Vercel (Hobby)

| Concern | Finding |
|---|---|
| ffmpeg runtime | **[REPORTED]** With Fluid Compute, Hobby max session duration is now 45 min (was 60 s) — so a trim *can* finish. But Hobby includes ~4 CPU-hours/month; ffmpeg re-encodes burn that budget in a handful of 10-min 720p clips. Function memory 1024 MB. |
| Filesystem | Read-only except `/tmp`; ephemeral per invocation. Same external-storage requirement as Render. |
| Cold start / spin-down | Serverless invocation model; no long-lived worker. Async job pattern requires external queue/DB polling (e.g., cron) — and **[REPORTED]** Hobby cron jobs run at most once per day, which is nearly useless for a job queue. |
| Response size limits | Request body ~4.5 MB (fine — we only submit URLs); responses must stream, and serving files repeatedly from a function is CPU-inefficient. |
| Outbound networking | Allowed. Same YouTube bot-check caveat. |
| Verdict | **Not recommended as the primary backend** for this workload. It remains the natural host if you later rebuild the frontend in Next.js — the backend API is framework-agnostic HTTP. |

### 1.3 Cloudflare

| Concern | Finding |
|---|---|
| Workers free | ~10 ms CPU per invocation. Native ffmpeg binaries cannot run (V8 isolates, no syscall interface); ffmpeg.wasm needs orders of magnitude more CPU. **A trimming backend cannot run on Workers free.** |
| Pages | Free static hosting (unlimited static bandwidth) — perfectly fine for the frontend if we ever split it out. Not needed in v1 since the backend serves the UI itself. |
| R2 | 10 GB free storage with zero egress fees, but **[REPORTED]** enabling R2 requires a payment card on file → violates the "no credit card" constraint, so it is NOT part of the default setup. Documented as an optional upgrade path. |

### 1.4 Video acquisition reality check (the honest part)

**[VERIFIED 2026-10-03]** From a datacenter IP:
- `api.cobalt.tools` (official cobalt instance) returns `400 error.api.auth.jwt.missing` — the public API now requires a JWT/API key.
- yt-dlp 2026.08.19 gets `Sign in to confirm you're not a bot` from YouTube across `tv_embedded`, `android_vr`, `web_embedded` player clients.

Implications, stated plainly:
1. The third-party downloader provider is implemented against the **cobalt API schema** (open-source, self-hostable, many community instances) with **instance URL + optional bearer key taken from environment variables**. You must supply your own instance/URL+key. Nothing is hardcoded, and no URLs are invented.
2. The yt-dlp provider supports an optional **cookies file** (`YTDLP_COOKIES_FILE` env) — the standard documented remedy for datacenter-IP bot checks. From a residential IP (e.g., running locally) it typically works with no cookies.
3. Third-party download services are unofficial and unstable by nature; they can change or break without notice (spec §5 acknowledges this). Every provider failure surfaces as a clear `failed` job status, never a crash.
4. For pipeline verification in this build environment (YouTube blocked, no cobalt key), a clearly-labeled **`sample` provider** synthesizes a local test video with ffmpeg. It exists for tests/demos only and is never enabled by accident in production (explicit env opt-in).

## 2. Databases

| Option | Finding |
|---|---|
| Render Postgres free | **[DOC]** 1 GB, single instance per workspace, **expires 30 days after creation** → disqualified for "history survives redeploys". |
| Neon free | **[REPORTED]** ~0.5 GB/project, ~100 CU-hours/month, compute auto-suspend after ~5 min idle with ~1 s cold resume, no credit card. Ample for thousands of job rows. **Chosen.** |
| Supabase free | 500 MB Postgres + 1 GB file storage; projects pause after ~1 week of inactivity. Solid alternative; noted in README. |
| SQLite on local disk | Fine for local dev/tests; wiped on every Render redeploy — production default is Neon. |

## 3. Recommended architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Render Free web service (Docker: python + ffmpeg + yt-dlp)  │
│                                                             │
│  FastAPI                                                    │
│  ├── REST API  /api/jobs, /api/styles, /api/health …        │
│  ├── Static UI /  (plain HTML/JS, same origin)              │
│  ├── In-process job worker (single, sequential)             │
│  ├── Downloader provider chain (pluggable):                 │
│  │     cobalt (3rd-party API) → yt-dlp (fallback) → sample  │
│  ├── Clip style registry (auto-discovered modules)          │
│  ├── Retention sweeper (default 24 h)                       │
│  └── AI extension point (protocol + stub, no logic)         │
│                                                             │
│  Ephemeral disk: /data/clips/*.mp4  (lost on redeploy —     │
│  accepted tradeoff; metadata survives in Neon)              │
└──────────────────┬──────────────────────────────────────────┘
                   │ SQLAlchemy (psycopg2)
                   ▼
          Neon free-tier Postgres (jobs table, migrations via Alembic)
```

**Component placement:**
- **Backend + frontend**: one Render free web service (single origin, no CORS, simplest free deployment).
- **Metadata**: Neon Postgres (free, cardless, survives redeploys).
- **Clip files**: ephemeral local disk + retention sweeper; `file_deleted_at` marker keeps history after deletion. Optional future upgrade: Supabase Storage or R2 (R2 needs a card — not default).

**What cannot be met, and the closest free alternative (no silent substitution):**
- Durable clip storage beyond redeploy windows is impossible on Render free (no persistent disks). Alternative: none cardless-and-simple; accepted tradeoff is re-clipping on demand since metadata (URL + timings) always survives.
- A dedicated always-on background worker is not free on Render. Alternative: in-process worker thread — jobs die if the service restarts mid-job; the app detects and marks these as `failed` with "service restarted during processing" so history stays truthful.
- Real-YouTube downloads from Render's datacenter IP will likely hit the same bot-check verified above unless you configure a cookies file or a working cobalt instance. This is a provider-level configuration matter, surfaced honestly in job errors and the README.

## 4. Risk register (top items)

| Risk | Mitigation built into v1 |
|---|---|
| Service spin-down kills in-flight job | Startup recovery marks stale `downloading/clipping` jobs `failed` with a clear message |
| 512 MB RAM ceiling | Single-worker queue, 720p height cap default, ffmpeg `veryfast` preset, strict clip-length cap |
| Provider breaks (site change, auth, block) | Provider chain with per-provider error capture; job fails with the provider's own message; providers swappable via env |
| Disk fill on ephemeral volume | Retention sweeper + size accounting; 24 h default |
| Abuse of the free instance | Per-IP sliding-window rate limits (job creation and general), request-size cap, URL allow-list validation |
| Neon cold resume (~1 s) | Acceptable for personal use; pooled connections |

## 5. Sign-off

The recommended setup uses **only free tiers, no paid services, no credit card** (Render free + Neon free). Vercel and Cloudflare are documented as evaluated-and-rejected for the backend with reasons (§1.2, §1.3). Proceeding to Phase 1 implementation on this architecture.
