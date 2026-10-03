"""Media proxy — same-origin relay for browser-engine downloads.

The browser engine resolves YouTube videos through vidssave.com directly
from the USER's IP (frontend/js/vidssave-client.js). vidssave's media CDN
sends no CORS headers, though, so the browser cannot read those bytes
cross-origin. This endpoint relays them: the backend (whose IP vidssave's
CDN happily serves — no IP locking) streams the remote file through with
Range passthrough, and the page fetches it same-origin.

The CDN (down-XX.vidssave.com, paths like /tmp/recycle/1m/...) serves files
that materialize lazily and recycle after use: a freshly issued link can
403 for the first ~30-60 s before the file appears (verified live
2026-10-03), and it also 403s non-browser User-Agents. The proxy therefore
identifies as a browser and RETRIES transient upstream refusals with a
backoff (mirroring the vidssave provider's proven download loop) instead of
failing over to a 502 that the <video> element cannot recover from.

Strictly allowlisted to vidssave hosts to avoid becoming an open proxy
(SSRF). Media streaming is exempt from the per-IP rate limiter (see
_MEDIA_PATH_RE in app.main) because <video> elements legitimately issue
many Range requests.
"""

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

router = APIRouter(tags=["media"])

# Allowed upstream hosts: vidssave API (the download_redirect endpoint) and
# any of its media CDN subdomains (down-de, down-sg, … — chosen by their 302).
_ALLOWED_SUFFIXES = (
    ".vidssave.com",
    ".vidssave.co",
)


def _allowed_upstream(url: str) -> bool:
    if not url.startswith("https://"):
        # sandbox-only extra hosts may be plain http (127.0.0.1 test servers)
        extra = _extra_hosts()
        host = urlsplit_host(url)
        if not (url.startswith("http://") and host in extra):
            return False
        return True
    host = urlsplit_host(url)
    if host == "vidssave.com" or host == "vidssave.co":
        return False  # bare registrable domain — not a media endpoint
    return host.endswith(_ALLOWED_SUFFIXES)


def _extra_hosts() -> set[str]:
    """Hosts (and host:port forms) allowed in addition to vidssave — testing only."""
    from app.config import get_settings

    raw = (get_settings().media_proxy_extra_hosts or "").strip()
    if not raw:
        return set()
    entries = set()
    for h in raw.split(","):
        h = h.strip().lower()
        if not h:
            continue
        entries.add(h)          # host[:port] as configured
        entries.add(h.split(":")[0])  # bare host form (httpx.URL.host has no port)
    return entries


def urlsplit_host(url: str) -> str:
    try:
        return httpx.URL(url).host or ""
    except Exception:
        return ""


# Headers relayed from the upstream response to the browser.
_PASSTHROUGH = ("content-type", "content-length", "content-range", "accept-ranges", "etag")

# Upstream statuses that are plausibly transient (CDN file not yet
# materialized, momentary WAF refusal) and retried with a backoff.
_RETRYABLE = {403, 408, 425, 429, 500, 502, 503, 504}
# ~62 s of total backoff — covers the observed ~30-60 s materialization window.
_RETRY_DELAYS = (0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


@router.get("/media/proxy")
def media_proxy(request: Request, url: str):
    if not url or not _allowed_upstream(url):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "forbidden_url",
                    "message": "Only vidssave media URLs may be proxied.",
                }
            },
        )

    # Upstream identity (verified live 2026-10-03 against production): the
    # vidssave CDN 403s non-browser User-Agents, AND from datacenter IPs
    # (like this server on Render) it also requires the request to look like
    # it comes from their own site — with Origin/Referer vidssave.com the
    # same datacenter IP downloads fine (the server provider has always
    # sent these and works from Render).
    upstream_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
        ),
        "Origin": "https://vidssave.com",
        "Referer": "https://vidssave.com/",
    }
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header

    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=15.0)
    client = httpx.Client(follow_redirects=True, timeout=timeout)
    upstream = None
    last_status = None
    try:
        for delay in _RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            req = client.build_request("GET", url, headers=upstream_headers)
            try:
                candidate = client.send(req, stream=True)
            except httpx.HTTPError:
                client.close()
                return JSONResponse(
                    status_code=502,
                    content={
                        "error": {
                            "code": "upstream_unreachable",
                            "message": "The media host could not be reached.",
                        }
                    },
                )
            if candidate.status_code < 400:
                upstream = candidate
                break
            last_status = candidate.status_code
            candidate.close()
            if last_status not in _RETRYABLE:
                break  # permanent refusal — report it right away
            # transient (CDN still materializing the file) — back off, retry
    except Exception:
        client.close()
        raise

    if upstream is None or upstream.status_code >= 400:
        client.close()
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "upstream_error",
                    "message": (
                        f"The media host responded with HTTP {last_status} "
                        "after several retries."
                        if last_status in _RETRYABLE
                        else f"The media host responded with HTTP {last_status}."
                    ),
                }
            },
        )

    headers = {
        k: v for k, v in upstream.headers.items() if k.lower() in _PASSTHROUGH
    }
    headers.setdefault("Cache-Control", "private, max-age=600")

    def _release() -> None:
        upstream.close()
        client.close()

    return StreamingResponse(
        upstream.iter_bytes(chunk_size=1 << 20),
        status_code=upstream.status_code,
        headers=headers,
        background=BackgroundTask(_release),
    )
