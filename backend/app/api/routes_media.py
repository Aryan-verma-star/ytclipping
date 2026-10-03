"""Media proxy — same-origin relay for browser-engine downloads.

The browser engine resolves YouTube videos through vidssave.com directly
from the USER's IP (frontend/js/vidssave-client.js). vidssave's media CDN
sends no CORS headers, though, so the browser cannot read those bytes
cross-origin. This endpoint relays them: the backend (whose IP vidssave's
CDN happily serves — no IP locking) streams the remote file through with
Range passthrough, and the page fetches it same-origin.

Strictly allowlisted to vidssave hosts to avoid becoming an open proxy
(SSRF). Media streaming is exempt from the per-IP rate limiter (see
_MEDIA_PATH_RE in app.main) because <video> elements legitimately issue
many Range requests.
"""

from __future__ import annotations

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

    upstream_headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Clipper/1.6"}
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header

    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=15.0)
    client = httpx.Client(follow_redirects=True, timeout=timeout)
    try:
        req = client.build_request("GET", url, headers=upstream_headers)
        upstream = client.send(req, stream=True)
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

    if upstream.status_code >= 400:
        upstream.close()
        client.close()
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "upstream_error",
                    "message": f"The media host responded with HTTP {upstream.status_code}.",
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
