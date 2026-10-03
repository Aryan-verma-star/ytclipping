# Deployment Guide — YouTube Clipper on Free Hosting (No Credit Card)

This guide takes you from "code on my machine" to "live URL I can share", using
only services with genuinely free tiers and **no credit card anywhere**.

**What you end up with**

```
                        ┌─────────────────────────────────────────┐
   your browser  ──────►│  Render free web service (Docker)       │
   https://…onrender.com│  ─ FastAPI backend + static frontend     │
                        │  ─ ffmpeg (inside the image)             │
                        │  ─ background downloads & clipping       │
                        └───────────────┬─────────────────────────┘
                                        │ (metadata only)
                        ┌───────────────▼─────────────────────────┐
                        │  Neon free Postgres                     │
                        │  ─ job/preview history, permanent       │
                        └─────────────────────────────────────────┘
```

**Total cost: $0/month.** Optional add-ons (Vercel for the frontend URL,
cron-job.org keep-alive pings) are also free.

---

## Why not everything on Vercel? (the short version)

Vercel is fantastic for static sites and short-lived functions — but this app's
backend is a **long-running process** that downloads videos for minutes, polls
status, and writes files to disk. Vercel's serverless model (functions frozen
between requests, no persistent disk, tight CPU-hour budget) is the wrong shape
for that workload regardless of the programming language — rewriting it in
Node/Next.js would hit exactly the same walls. Full analysis:
[`docs/feasibility-report.md`](feasibility-report.md).

So:

- **Backend → Render** (free Docker web service, real process, real disk)
- **Database → Neon** (free Postgres, never expires)
- **Frontend → served by the backend itself** (one service, one URL, no CORS)
  — with an *optional* Vercel front if you want a `*.vercel.app` URL (§5)

No code changes needed for either path. The app is already Dockerized.

---

## 1. Accounts you need (all free, none ask for a card)

| Service   | Purpose                        | Free tier                                | Card? |
|-----------|--------------------------------|------------------------------------------|-------|
| GitHub    | hosts the code (deploys from it) | unlimited private repos                 | No   |
| Render    | runs the backend + frontend    | 750 instance-hours/month (≈ always-on for 1 service) | No |
| Neon       | Postgres database (metadata)   | ~0.5 GB storage, never expires           | No   |
| Vercel *(optional)* | static frontend on a nicer URL | Hobby plan | No |

> Sign-up tip: sign in to **Render with GitHub** — one identity, and it
> connects your repo in one click. Neon has its own login
> (Google / GitHub / e-mail all work).

---

## 2. The code on GitHub

Render builds from a Git repo, so the code lives on GitHub:

**https://github.com/Aryan-verma-star/ytclipping** — private repo, branch
`main`, already pushed.

**If you ever need to re-push it yourself** (e.g. from the
`ytclipper-render-deploy.zip` backup), the easiest way is a **Personal
Access Token**: github.com → avatar → **Settings** → **Developer settings** →
**Personal access tokens** → *Tokens (classic)* → **Generate new token** →
check `repo` → then:

```bash
cd ytclipper-backend          # the project folder (with backend/, frontend/, Dockerfile)
git init -b main
git add .
git commit -m "YouTube Clipper — ready to deploy"
git remote add origin https://github.com/Aryan-verma-star/ytclipping.git
git push -u origin main       # username = your GitHub login, password = the token
```

The included `.gitignore` already keeps the repo clean and safe — it excludes:

- `backend/data/` (local test clips, previews, SQLite files)
- `.env` files (secrets; only `.env.example` is committed)
- `cookies.txt` (never commit YouTube login cookies)
- sandbox scaffolding (the Next.js preview shell used during development)

**Quick sanity check before moving on** — the repo should contain exactly:

```
backend/        frontend/       docs/           scripts/
Dockerfile      render.yaml     README.md       .env.example    .gitignore
```

---

## 3. Create the Neon database (≈ 5 minutes)

1. Go to **neon.tech** → **Sign Up** (Google or GitHub login works).
2. **Create project** → name: `youtube-clipper` → Region: **Singapore**
   (closest to India; any region works) → Create.
3. The project dashboard shows a **Connection string**. Click copy. It looks
   like:
   ```
   postgresql://neondb_owner:xxxxxxxx@ep-xxxx-xxxx-123456.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```
4. Keep `?sslmode=require` at the end — it's required. Save this string
   somewhere for the next step.

**Free-tier behavior to expect:** Neon auto-suspends the database after ~5
minutes of inactivity and wakes it in ~1 second on the next request. You'll
never notice. It never expires and needs no card.

> **Which connection string?** Neon shows a *pooled* and a *direct* one.
> Either works for this app (it's a single long-running process, not
> serverless); the plain **direct** string is the simplest choice.

---

## 4. Deploy on Render — Path A: one service (recommended)

### 4.1 Create the service from the blueprint

1. Go to **render.com** → **Get Started** / **Sign In** — choose **GitHub**
   (or sign up with e-mail, then connect GitHub from User Settings →
   Connected accounts).
2. Grant Render access — the repo is **private**, so pick
   **Only select repositories** → `Aryan-verma-star/ytclipping`.
3. Dashboard → **New +** → **Blueprint**.
4. Select the `ytclipping` repo → Render reads `render.yaml`
   (already in the repo — it defines the service, region, health check and
   every environment variable).
5. Render prompts for the values marked `sync: false`. Fill in:

   | Prompt | Value |
   |---|---|
   | `CLIPPER_DATABASE_URL` | **paste your Neon connection string** (required) |
   | `CLIPPER_COBALT_API_URL` / `_KEY` | leave empty for now (§6) |
   | `CLIPPER_YTDLP_COOKIES_FILE` / `CLIPPER_YTDLP_COOKIES` | leave empty for now (§6) |
   | `CLIPPER_ALLOWED_ORIGINS` | leave empty (only needed for §5) |

6. Click **Apply**. Render builds the Docker image (installs ffmpeg + Python
   dependencies). **First build takes ~4–7 minutes** — later builds are faster
   thanks to layer caching.
7. When it goes live, your app is at **`https://youtube-clipper.onrender.com`**
   (exact URL is shown at the top of the service page).

### 4.2 Verify it worked

- Open **`https://YOUR-APP.onrender.com/api/health`** → you should see:
  ```json
  {"status": "ok", "checks": {"database": "ok", "ffmpeg": "ok", "job_worker": "running"}, ...}
  ```
  `database: ok` proves the Neon wiring worked. If not, see §10.
- Open **`https://YOUR-APP.onrender.com/`** → the clipper editor UI loads.
  On the very first visit the page may take 30–60 s — that's the free tier
  waking up (§7 fixes this).

### 4.3 Smoke test with the sample provider (no YouTube needed)

The app ships a built-in `sample` provider that synthesizes a video locally —
perfect for proving the whole pipeline works before touching real YouTube:

1. Render dashboard → your service → **Environment** → edit
   `CLIPPER_DOWNLOADER_PROVIDERS` → change the value to `sample` → **Save**.
   (Saving triggers a quick redeploy, ~1–2 min.)
2. Open the UI, paste **any** URL (it's ignored by the sample provider), and
   run the full flow: instant timeline → select a range → create clip →
   download the 9:16 result.
3. When it works, go back to **Environment** and set
   `CLIPPER_DOWNLOADER_PROVIDERS` back to `cobalt,ytdlp`.

---

## 5. Optional — Path B: frontend on Vercel, backend on Render

Only do this if you specifically want the frontend on a `*.vercel.app` URL
(nicer domain, Vercel's deploy previews). The single-service Path A above is
simpler and needs none of this.

Both pieces are still free; they're just deployed separately:

1. **Point the frontend at your Render backend.** Edit `frontend/index.html`
   and set the config line near the top:
   ```js
   window.CLIPPER_API_BASE = "https://youtube-clipper.onrender.com";  // your Render URL
   ```
   Commit + push.
2. **Deploy the frontend on Vercel.** vercel.com → **Add New… → Project** →
   Import the `ytclipping` repo from GitHub → set **Root Directory** to
   `frontend` → Framework Preset: **Other** → **Deploy**.
   (Vercel serves the static files as-is; there is nothing to build.)
3. **Allow that origin on the backend.** Render → your service →
   **Environment** → add:
   ```
   CLIPPER_ALLOWED_ORIGINS = https://youtube-clipper.vercel.app
   ```
   (your actual Vercel URL, no trailing slash) → Save. This switches on CORS
   for exactly that origin — the backend already supports it.
4. Open the Vercel URL and repeat the §4.2 health/usage checks. Note that the
   page loads from Vercel but **all API and video traffic goes to Render** —
   which is why Render stays the piece that matters.

Every later `git push` automatically redeploys both: Render (backend) and
Vercel (frontend).

---

## 6. Enable real YouTube downloads

Render runs on datacenter IPs, and YouTube bot-checks anonymous datacenter
traffic (verified during development — this is a YouTube-side policy, not a
bug). Two supported ways through it, either or both:

### Option A — a cobalt instance (primary provider)

If you run or trust a cobalt API instance: Render → **Environment** → set
`CLIPPER_COBALT_API_URL` (and `CLIPPER_COBALT_API_KEY` if it needs auth) →
Save. The app tries cobalt first, yt-dlp second.

### Option B — yt-dlp with your YouTube cookies (easiest)

The app's yt-dlp provider accepts your logged-in YouTube cookies pasted
straight into an environment variable:

1. In the browser where you're logged in to YouTube, install a cookies export
   extension — e.g. **"Get cookies.txt LOCALLY"** (Chrome/Web Store, Firefox
   Add-ons; pick one with a good rating and local-only processing).
2. Visit `youtube.com`, click the extension, **Export** → you get a
   `cookies.txt` file (Netscape format).
3. Open the file in any text editor, **select all, copy**.
4. Render → **Environment** → Add Environment Variable:
   - Key: `CLIPPER_YTDLP_COOKIES`
   - Value: *paste the file contents*
   → **Save** (redeploys; the app writes the cookies to a private 0600 file
   inside the container at startup).
5. Test with a short video.

Notes:

- **Security:** cookies = your YouTube login. That's why the repo is private
  and the env var is stored encrypted by Render. Never commit `cookies.txt`
  to the repo (`.gitignore` blocks it as a safety net).
- **Expiry:** YouTube rotates cookies every few weeks/months. When downloads
  suddenly start failing with the bot-check message again, re-export and
  re-paste — 30 seconds of work.
- From a **home/residential IP** (running locally) yt-dlp typically works
  with no cookies at all — this is only a datacenter-IP concern.

---

## 7. Keep the free service awake (optional but nice)

Render free services **spin down after ~15 minutes without traffic**; the next
request waits ~30–60 s while it wakes. Two ways to deal with it:

- **Do nothing** — fine for occasional personal use; the first load is just
  slow.
- **Free keep-alive ping** — create an account at **cron-job.org** (free, no
  card) → **Create cronjob** → URL: `https://YOUR-APP.onrender.com/api/health`,
  schedule: **every 10 minutes** → Save. The service never sleeps, so every
  visit is instant. (UptimeRobot with 5-minute HTTP checks works too.)

The free plan's 750 instance-hours/month ≈ 31 days × 24 h — exactly enough for
one always-on service, so keep-alive costs nothing extra.

---

## 8. Updating the app later

```bash
git add . && git commit -m "describe the change" && git push
```

Render's **autoDeploy** (enabled in `render.yaml`) rebuilds and redeploys
automatically (~3–6 min). Vercel redeploys too if you set up §5. Environment
variables survive redeploys — you never re-enter them.

**What a redeploy resets:** files on the container (downloaded sources,
generated clips) are wiped, and any in-flight download/clip job is marked
`failed` with a clear "service restarted" message — just resubmit it. Metadata
(history) lives in Neon and survives everything.

---

## 9. Free-tier limits — the honest table

| What | Free limit | Effect on this app |
|---|---|---|
| Render instance hours | 750 h/month | One always-on service fits exactly |
| Render RAM | 512 MB | Fine — the app caps sources at 720p by design |
| Render disk | Ephemeral (wiped on redeploy/restart) | **Download your clip when it's ready**; a 24 h sweeper cleans up anyway |
| Render spin-down | After ~15 min idle | 30–60 s cold start unless you add the §7 ping |
| Neon storage | ~0.5 GB | Years of job history — it only stores metadata |
| Neon compute | Auto-suspends when idle | ~1 s extra on the first request after a pause — invisible |
| Bandwidth | Render free includes 100 GB/month outbound | A personal clipper uses a tiny fraction |

If you ever outgrow this (persistent clips, more RAM, no cold starts), the
upgrade path is the same app on a Render paid instance or any Docker host —
zero code changes.

---

## 10. Troubleshooting

| Symptom | Cause → Fix |
|---|---|
| `/api/health` shows `database: error` or the deploy fails to go live | `CLIPPER_DATABASE_URL` wrong: must start with `postgresql://`, include `?sslmode=require`, and use the `CLIPPER_` prefix (the app deliberately ignores the generic `DATABASE_URL`). Re-check the Neon dashboard string. |
| First page load takes ~1 minute | Free-tier cold start — expected. Add the §7 keep-alive ping. |
| Download fails with "YouTube is bot-checking this server's IP" | Expected from datacenter IPs without cookies — set `CLIPPER_YTDLP_COOKIES` (§6 Option B) or a working `CLIPPER_COBALT_API_URL`. |
| Everything worked, then broke weeks later | YouTube rotated your cookies — re-export and re-paste (§6). |
| Browser console shows CORS errors (only in §5 split setup) | Forgot one of the two split-config steps: `window.CLIPPER_API_BASE` in `frontend/index.html`, and `CLIPPER_ALLOWED_ORIGINS` on Render. Both must match URLs exactly (https, no trailing slash). |
| Build fails on Render | Check the build log. The repo must contain `backend/`, `frontend/`, `Dockerfile` at the root (§2's sanity check). |
| Clip link returns `410 Gone` | File passed its 24 h retention window or the service restarted — the file is gone, the record stays. Create the clip again. |
| `429 Rate limit exceeded` | Per-IP protection (personal-use design). Wait ~60 s. |
| Health `ok` but UI shows provider errors on paste | Providers unconfigured — that's §6, or switch to `sample` to test the pipeline (§4.3). |

Still stuck? The service's **Logs** tab (Render dashboard) shows exactly what
the backend did, including the friendly error messages the app raises.
