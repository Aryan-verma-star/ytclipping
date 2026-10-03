/* YouTube CORS pass-through — the "network identity" shift (Pattern B).
 *
 * A deliberately DUMB proxy: it never extracts, parses or re-signs anything.
 * The browser's client engine owns all resolution logic (innertube contexts,
 * format picking). This worker only:
 *
 *   POST /yti      → forwards a JSON innertube request to www.youtube.com
 *   GET  /stream   → pipes *.googlevideo.com bytes back with CORS + Range
 *   OPTIONS  *     → CORS preflight
 *
 * Deploy (production):  Cloudflare Worker — see proxy/DEPLOY.md
 * Run (dev/sandbox):    node proxy/dev-server.mjs   (same fetch handler)
 *
 * Egress IP note: requests to YouTube leave from this worker's network
 * (Cloudflare edge in production, your machine in dev), NOT from the user.
 * The companion Chrome extension (deploy/extension) is the variant where
 * the user's own IP is used end-to-end.
 */

const YOUTUBE_HOST = "https://www.youtube.com";
const STREAM_HOST_RE = /^https:\/\/[a-z0-9.-]+\.googlevideo\.com\//i;
const YTI_PATH_RE = /^\/youtubei\/[a-z0-9/_-]+$/i;
const MAX_YTI_BODY = 16 * 1024;

/* Per-IP sliding window: 120 requests / minute (a chunked download makes
 * a few dozen GETs; resolution makes 1-4 POSTs). Kept in memory — good
 * enough for an abuse backstop; Cloudflare recycles isolates anyway. */
const RATE_LIMIT = 120;
const RATE_WINDOW_MS = 60_000;
const rateBuckets = new Map();

function rateLimited(ip) {
  const now = Date.now();
  let bucket = rateBuckets.get(ip);
  if (!bucket || now - bucket.t0 > RATE_WINDOW_MS) {
    if (rateBuckets.size > 10_000) rateBuckets.clear();
    bucket = { t0: now, n: 0 };
    rateBuckets.set(ip, bucket);
  }
  bucket.n += 1;
  return bucket.n > RATE_LIMIT;
}

function clientIp(request) {
  return (
    (request.headers && (request.headers.get("cf-connecting-ip") ||
      request.headers.get("x-forwarded-for"))) ||
    "local"
  );
}

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Range, Content-Type, Accept",
  "Access-Control-Max-Age": "86400",
  "Access-Control-Expose-Headers":
    "Content-Range, Content-Length, Accept-Ranges, Content-Type, X-Resolve-Via",
};

function json(body, status, extraHeaders) {
  return new Response(JSON.stringify(body), {
    status: status || 200,
    headers: Object.assign({ "Content-Type": "application/json" }, CORS, extraHeaders || {}),
  });
}

/* ------------------------------ /yti ------------------------------ */

async function handleYti(request) {
  if (request.method !== "POST") return json({ error: "POST only" }, 405);

  let payload;
  try {
    payload = await request.json();
  } catch (e) {
    return json({ error: "body must be JSON" }, 400);
  }
  const path = String(payload.path || "");
  const body = payload.body == null ? {} : payload.body;
  const headers = payload.headers && typeof payload.headers === "object" ? payload.headers : {};

  if (!YTI_PATH_RE.test(path)) return json({ error: "path not allowed" }, 400);
  if (JSON.stringify(body).length > MAX_YTI_BODY) return json({ error: "body too large" }, 413);

  const outHeaders = { "Content-Type": "application/json", "Accept": "*/*" };
  for (const key of ["User-Agent", "X-YouTube-Client-Name", "X-YouTube-Client-Version", "X-Goog-Visitor-Id", "Accept-Language"]) {
    if (typeof headers[key] === "string" && headers[key].length < 512) outHeaders[key] = headers[key];
  }

  const upstream = await fetch(YOUTUBE_HOST + path, {
    method: "POST",
    headers: outHeaders,
    body: JSON.stringify(body),
  });

  const text = await upstream.text();
  return new Response(text, {
    status: upstream.status,
    headers: Object.assign(
      { "Content-Type": "application/json", "X-Resolve-Via": "proxy" },
      CORS
    ),
  });
}

/* ----------------------------- /stream ----------------------------- */

async function handleStream(request, url) {
  if (request.method !== "GET" && request.method !== "HEAD") return json({ error: "GET only" }, 405);
  const target = url.searchParams.get("u");
  if (!target) return json({ error: "missing u parameter" }, 400);
  let decoded;
  try {
    decoded = decodeURIComponent(target);
  } catch (e) {
    decoded = target;
  }
  if (!STREAM_HOST_RE.test(decoded)) return json({ error: "host not allowed" }, 400);

  const outHeaders = {};
  const range = request.headers.get("Range");
  if (range) outHeaders["Range"] = range;

  const upstream = await fetch(decoded, { headers: outHeaders });
  if (!upstream.ok && upstream.status !== 206) {
    return json({ error: "upstream " + upstream.status }, 502);
  }

  const respHeaders = Object.assign({}, CORS);
  for (const key of ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"]) {
    const value = upstream.headers.get(key);
    if (value) respHeaders[key] = value;
  }
  return new Response(upstream.body, { status: upstream.status, headers: respHeaders });
}

/* ------------------------------ entry ------------------------------ */

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    if (rateLimited(clientIp(request))) {
      return json({ error: "rate limited" }, 429, { "Retry-After": "30" });
    }

    try {
      if (url.pathname === "/yti") return await handleYti(request);
      if (url.pathname === "/stream") return await handleStream(request, url);
      return json({ ok: true, service: "yt-clipper-cors-proxy" });
    } catch (err) {
      return json({ error: String((err && err.message) || err) }, 502);
    }
  },
};
