# Browser-engine proxy — deploy guide

The YouTube Clipper frontend ships a **browser engine**: stream resolution,
the download, and the 9:16 clip (ffmpeg.wasm) all run on the user's device,
so no server IP can be banned by YouTube. The only piece that cannot run in
the page is cross-origin access to `youtube.com` / `googlevideo.com` — the
browser's same-origin policy blocks it. This tiny proxy solves exactly that
and nothing else.

```
browser ── POST /yti ────► worker ──► www.youtube.com/youtubei/v1/player
browser ── GET  /stream?u=<googlevideo-url>  ►  worker ──► googlevideo (Range-aware)
```

It never extracts, parses, or re-signs anything — it is a dumb CORS pipe
with a per-IP rate limit (120 req/min). Two interchangeable runtimes ship
the same fetch handler:

| File | Runtime | Use |
|---|---|---|
| `worker.js` | Cloudflare Workers | **production** |
| `dev-server.mjs` | Node ≥ 18 | local dev / build sandbox (`node proxy/dev-server.mjs`, port 8020) |

## Deploy to Cloudflare (free tier, ~2 minutes, no CLI needed)

1. Sign up / log in at <https://dash.cloudflare.com> (free plan is enough).
2. **Workers & Pages → Create → Create Worker → Deploy** (accept the default
   name or pick one like `yt-clipper-proxy`).
3. Click **Edit code**, delete the boilerplate, paste the entire contents of
   `proxy/worker.js`, **Deploy**.
4. Copy the worker URL, e.g. `https://yt-clipper-proxy.your-name.workers.dev`.
5. Open `frontend/index.html` in the repo and set:
   ```js
   window.CLIPPER_YT_PROXY = "https://yt-clipper-proxy.your-name.workers.dev";
   ```
   (no trailing slash). Redeploy the frontend — the header chip now offers
   `engine: browser`.

### Wrangler CLI alternative

```bash
npm create cloudflare@latest yt-clipper-proxy   # accept defaults, "Hello World" starter
cd yt-clipper-proxy
# replace src/index.js with proxy/worker.js, then:
npx wrangler deploy
```

## Notes

- **Egress IP**: requests to YouTube leave from Cloudflare's edge, not from
  your Render server and not from the user. Cloudflare's IP reputation is
  generally good enough for the innertube `player` endpoint; if a video
  demands a login ("bot check"), the UI surfaces it and the user can fall
  back to the server engine or the extension.
- **Rate limit**: 120 requests/minute per IP, in-memory (Cloudflare
  recycles isolates, so this is a backstop, not a guarantee).
- **Allowed hosts**: `/stream` only pipes `*.googlevideo.com`; `/yti` only
  forwards `/youtubei/...` paths. Nothing else can be proxied.
- **Free tier limits** (as of writing): 100k requests/day — a clip session
  uses ~5–60 of them.

## Companion Chrome extension (optional, no proxy needed)

The other transport (`Pattern A`) is a Chrome extension that performs the
innertube call and the ranged downloads from the user's own residential IP,
with the user's real browser fingerprint — zero third-party infrastructure.
The frontend already speaks its bridge contract:

```js
window.__YTCP__ = {
  version: 1,
  yti(path, body, headers)        // -> Promise<playerJSON>
  fetchRange(url, rangeHeader)    // -> Promise<Response>
};
```

A content script injects this object on the app's origin; the client engine
picks it up automatically (it takes priority over the proxy). The extension
itself is not yet included in this repo — build one with a manifest v3
background service worker and `host_permissions` for
`*://*.youtube.com/*` and `*://*.googlevideo.com/*`.
