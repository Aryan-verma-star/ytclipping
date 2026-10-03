"""vidssave.com provider — reverse-engineered third-party resolver (spec §5).

Why: YouTube bot-checks datacenter IPs (Render/Neon egress), which kills
server-side yt-dlp. vidssave.com resolves + muxes the file on ITS
infrastructure and hands back a CDN link that is NOT IP-locked, so any
server (or the user's browser — see frontend/js/vidssave-client.js, the
primary path) can fetch the bytes.

Protocol (reverse-engineered 2026-10-03 from vidssave.com's JS bundle):
  1. POST {api}/media/parse      origin=source, link=<yt url>
     -> {status, data: <AES JSON>} with {title, duration, resources[]};
        each video resource carries an opaque `resource_content` token
  2. POST {api}/media/download   request=<resource_content>, no_encrypt=1
     -> {status, data: <AES JSON>} with {task_id}
  3. GET  {sse}/media/download_query?task_id=...  (Server-Sent Events)
     -> events: running{progress} / success{download_link, filesize} /
        failed   — the task muxes video+audio server-side
  4. GET  download_link -> 302 -> signed CDN mp4 (any IP, Range-capable)

The `data` fields are AES-256-CBC encrypted (base64, ZeroPadding) with the
key candidates embedded in their frontend; decryption mirrors their
readResponse() chain (plain JSON first, then decrypt).

API hosts: production first; their staging endpoint (test-api.vidssave.com,
which currently tolerates datacenter IPs) as fallback — both were verified
live. CAUTION: third-party download services are unofficial and can change
or break without notice; failures surface as job errors, never crashes.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.core.ffmpeg import ffprobe_duration
from app.downloader.base import (
    DownloaderProvider,
    ProgressCB,
    ProviderError,
    VideoSource,
)

log = logging.getLogger("clipper.vidssave")

_uid = itertools.count(1)

# --- endpoint constants (extracted from the site's JS, verified live) -------
API_PROD = "https://api.vidssave.com/api/contentsite_api"
API_DEV = "https://test-api.vidssave.com/vapi/contentsite_api"
SSE_PROD = "https://api.vidssave.com/sse/contentsite_api"
SSE_DEV = "https://test-api.vidssave.com/vsse/contentsite_api"

FORM_BASE = {
    "hostname": "vidssave.com",
    "auth": "4c9b7d21",
    "domain": "api-ak.vidssave.com",
}
SSE_QUERY = {
    "auth": "20250901majwlqo",
    "domain": "api-ak.vidssave.com",
    "download_domain": "vidssave.com",
    "origin": "content_site",
}

# AES key candidates + IV derivation, exactly as the site's JS does:
#   key = Utf8(keyStr), iv = Utf8(keyStr[:16]), AES-CBC, ZeroPadding
AES_KEYS = [
    ("4c9b7d2e" * 3) + "4c9b7d21",  # 32 bytes -> AES-256 (primary)
    "rz18efAXUbdiaO7k",  # 16 bytes -> AES-128 (fallback)
]

_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


class _TransientUpstream(Exception):
    """Retryable upstream refusal (CDN 403/42x, truncated stream)."""


_DEFAULT_HEADERS = {
    # The vidssave CDN 403s non-browser User-Agents (found live 2026-10-03),
    # so every request — including the CDN download — identifies as a browser.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "Origin": "https://vidssave.com",
    "Referer": "https://vidssave.com/",
}


def _decrypt(b64_text: str) -> str | None:
    """AES-CBC/ZeroPadding decrypt of a base64 payload; None when impossible."""
    from Crypto.Cipher import AES  # pycryptodome

    text = (b64_text or "").strip()
    if not text or len(text) % 4 != 0 or not _BASE64_RE.match(text):
        return None
    try:
        ciphertext = base64.b64decode(text)
    except Exception:
        return None
    if not ciphertext or len(ciphertext) % 16 != 0:
        return None
    for key in AES_KEYS:
        if len(key) not in (16, 24, 32):
            continue
        try:
            plain = AES.new(
                key.encode(), AES.MODE_CBC, key[:16].encode()
            ).decrypt(ciphertext)
            plain = plain.rstrip(b"\x00")  # ZeroPadding
            if not plain:
                continue
            return plain.decode("utf-8")
        except Exception:
            continue  # wrong key — try the next candidate
    return None


def unwrap(payload) -> dict:
    """Mirror of the site's readResponse(): plain JSON first, then decrypt."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except Exception:
            decrypted = _decrypt(payload if isinstance(payload, str) else payload.decode("utf-8", "replace"))
            if decrypted is None:
                raise ProviderError(
                    "vidssave returned an undecodable response (site changed?).",
                    provider="vidssave",
                )
            try:
                payload = json.loads(decrypted)
            except Exception:
                raise ProviderError(
                    "vidssave decrypted response was not JSON.",
                    provider="vidssave",
                )
    if (
        isinstance(payload, dict)
        and payload.get("status") == 1
        and isinstance(payload.get("data"), str)
    ):
        text = payload["data"].strip()
        try:
            payload["data"] = json.loads(text)
            return payload
        except Exception:
            decrypted = _decrypt(text)
            if decrypted is not None:
                try:
                    payload["data"] = json.loads(decrypted)
                except Exception:
                    payload["data"] = decrypted
    return payload


def _quality_number(quality: str) -> int:
    match = re.match(r"(\d{3,4})\s*p", str(quality or ""), re.IGNORECASE)
    return int(match.group(1)) if match else 0


class VidsSaveProvider(DownloaderProvider):
    name = "vidssave"
    supports_reuse = False  # the URL only exists after a muxing task

    def __init__(self, settings) -> None:
        self.settings = settings

    # -- low-level helpers --------------------------------------------------

    def _api_hosts(self) -> list[tuple[str, str]]:
        """[(api_base, sse_base), ...] — explicit override or prod+dev."""
        override = (self.settings.vidssave_api_url or "").strip().rstrip("/")
        if override:
            return [(override, override.replace("/api/", "/sse/").replace("/vapi/", "/vsse/"))]
        return [(API_PROD, SSE_PROD), (API_DEV, SSE_DEV)]

    def _post_form(self, api_base: str, path: str, fields: dict) -> dict:
        timeout = httpx.Timeout(
            connect=10.0,
            read=self.settings.vidssave_timeout_seconds,
            write=15.0,
            pool=10.0,
        )
        body = dict(FORM_BASE)
        body.update(fields)
        try:
            response = httpx.post(
                api_base + path,
                data=body,
                headers=_DEFAULT_HEADERS,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"network error talking to vidssave: {exc}",
                provider=self.name,
                transient=True,
            ) from exc
        if response.status_code == 429:
            raise ProviderError(
                "vidssave rate-limited the request; retry later.",
                provider=self.name,
                transient=True,
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"vidssave API returned HTTP {response.status_code}.",
                provider=self.name,
            )
        try:
            return unwrap(response.json())
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                "vidssave API returned a non-JSON body (site changed?).",
                provider=self.name,
            ) from exc

    def _parse(self, url: str) -> dict:
        """media/parse with prod→dev fallback; returns the decrypted data."""
        last_error: ProviderError | None = None
        for api_base, _sse in self._api_hosts():
            envelope = self._post_form(api_base, "/media/parse", {"origin": "source", "link": url})
            if envelope.get("status") == 1 and isinstance(envelope.get("data"), dict):
                return envelope["data"]
            code = str(envelope.get("status_code") or envelope.get("msg") or "unknown")
            last_error = ProviderError(
                f"vidssave could not analyze this URL ({code}).",
                provider=self.name,
            )
            log.info("vidssave parse failed on %s: %s", api_base, code)
        raise last_error or ProviderError("vidssave analysis failed.", provider=self.name)

    def _pick_resource(self, data: dict) -> tuple[dict, str]:
        """Best video resource not above max_video_height (else the best)."""
        videos = [
            r
            for r in (data.get("resources") or [])
            if isinstance(r, dict) and r.get("type") == "video" and r.get("resource_content")
        ]
        if not videos:
            raise ProviderError(
                "vidssave returned no downloadable video formats.",
                provider=self.name,
            )
        cap = int(self.settings.max_video_height)
        videos.sort(  # highest quality first (order is not guaranteed upstream)
            key=lambda r: _quality_number(r.get("quality")), reverse=True
        )
        pick = None
        for r in videos:
            height = _quality_number(r.get("quality"))
            if height and height <= cap:
                pick = r
                break
        if pick is None:
            pick = videos[0]
        host = "prod"
        return pick, host

    def _poll_task(
        self,
        sse_base: str,
        task_id: str,
        progress: ProgressCB | None = None,
    ) -> tuple[str, int | None]:
        """SSE poll via a plain streaming GET; returns (download_link, size)."""
        params = dict(SSE_QUERY)
        params["task_id"] = task_id
        deadline = time.monotonic() + self.settings.vidssave_task_timeout_seconds
        timeout = httpx.Timeout(
            connect=10.0,
            read=60.0,  # between SSE frames
            write=10.0,
            pool=10.0,
        )
        event = ""
        try:
            with httpx.stream(
                "GET",
                sse_base + "/media/download_query",
                params=params,
                headers={**_DEFAULT_HEADERS, "Accept": "text/event-stream"},
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    raise ProviderError(
                        f"vidssave task stream returned HTTP {response.status_code}.",
                        provider=self.name,
                    )
                for raw_line in response.iter_lines():
                    if time.monotonic() > deadline:
                        raise ProviderError(
                            "vidssave muxing task timed out.",
                            provider=self.name,
                            transient=True,
                        )
                    line = raw_line.rstrip("\r")
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        payload = line.split(":", 1)[1].strip()
                        if event == "success":
                            try:
                                data = json.loads(payload)
                            except Exception as exc:
                                raise ProviderError(
                                    "vidssave finished the task with a malformed payload.",
                                    provider=self.name,
                                ) from exc
                            link = data.get("download_link")
                            if not link:
                                raise ProviderError(
                                    "vidssave finished the task without a download link.",
                                    provider=self.name,
                                )
                            return link, data.get("filesize")
                        if event == "failed":
                            raise ProviderError(
                                "vidssave could not prepare this file (task failed).",
                                provider=self.name,
                            )
                        if event == "running" and progress is not None:
                            try:
                                data = json.loads(payload)
                                pct = float(data.get("progress") or 0)
                                progress(min(0.95, pct / 100.0))
                            except (ValueError, TypeError):
                                pass  # malformed progress frame — keep polling
                        # unknown events are ignored
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"network error while waiting for the vidssave task: {exc}",
                provider=self.name,
                transient=True,
            ) from exc
        raise ProviderError(
            "vidssave task stream ended without a result.",
            provider=self.name,
        )

    def _cancel_task(self, api_base: str, task_id: str) -> None:
        try:
            self._post_form(api_base, "/media/download_cancel", {"task_id": task_id})
        except Exception:  # fire-and-forget cleanup
            log.debug("vidssave task cancel failed (ignored)", exc_info=True)

    def _download_to_file(
        self,
        download_link: str,
        target: Path,
        progress: ProgressCB | None = None,
    ) -> None:
        """Stream the muxed file to disk with retry + Range resume.

        Their CDN intermittently 403s perfectly good signed links (observed
        live on the staging pipeline), so transient failures retry with a
        backoff and resume from the bytes already on disk.
        """
        cap = int(self.settings.max_download_bytes)
        timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
        retryable = {403, 408, 429, 500, 502, 503, 504}
        max_tries = 5

        written = 0
        total: int | None = None
        last_report = 0.0
        while True:
            headers = dict(_DEFAULT_HEADERS)
            if written > 0:
                headers["Range"] = f"bytes={written}-"
            try:
                with httpx.stream(
                    "GET", download_link, timeout=timeout, follow_redirects=True,
                    headers=headers,
                ) as resp:
                    if resp.status_code in retryable and written == 0:
                        # fresh 403/42x before any byte landed — plain retry
                        raise _TransientUpstream(resp.status_code)
                    if resp.status_code >= 400:
                        raise ProviderError(
                            f"downloading from the vidssave CDN failed with HTTP {resp.status_code}.",
                            provider=self.name,
                        )
                    if resp.status_code == 200 and written > 0:
                        # resume refused (whole body from 0) — retry, then fail
                        raise _TransientUpstream(resp.status_code)
                    if resp.status_code == 206:
                        content_range = resp.headers.get("content-range", "")
                        if "/" in content_range:
                            try:
                                total = int(content_range.rsplit("/", 1)[1])
                            except ValueError:
                                pass
                    elif resp.status_code == 200 and resp.headers.get("content-length", "").isdigit():
                        total = int(resp.headers["content-length"])
                    with open(target, "ab" if written else "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=1 << 20):
                            written += len(chunk)
                            if written > cap:
                                raise ProviderError(
                                    f"download exceeded MAX_DOWNLOAD_BYTES ({cap}); "
                                    "source video too large for the configured cap.",
                                    provider=self.name,
                                )
                            fh.write(chunk)
                            if progress is not None and time.monotonic() - last_report >= 0.5:
                                last_report = time.monotonic()
                                progress((written / total) if total else None)
                if written == 0:
                    raise ProviderError("vidssave CDN produced an empty file.", provider=self.name)
                if total is not None and written < total:
                    # stream ended early — loop continues and resumes
                    raise _TransientUpstream(0)
                break  # complete
            except _TransientUpstream:
                max_tries -= 1
                if max_tries <= 0:
                    raise ProviderError(
                        "vidssave CDN kept refusing the download after several retries.",
                        provider=self.name,
                        transient=True,
                    )
                time.sleep(min(4.0, 0.8 * (6 - max_tries)))
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"network error while downloading from vidssave: {exc}",
                    provider=self.name,
                    transient=True,
                ) from exc
        if progress is not None:
            progress(1.0)

    # -- interface --------------------------------------------------------

    def resolve_stream(self, url: str, start: float, end: float):
        """Not cheap: the URL only exists after a server-side muxing task."""
        return None

    def get_video(
        self,
        url: str,
        start: float,
        end: float,
        progress: ProgressCB | None = None,
    ) -> VideoSource:
        if not self.settings.vidssave_enabled:
            raise ProviderError(
                "vidssave provider is disabled (CLIPPER_VIDSSAVE_ENABLED=false).",
                provider=self.name,
            )

        data = self._parse(url)
        resource, _host = self._pick_resource(data)
        title = str(data.get("title") or "video")

        # create the muxing task on whichever API host parsed successfully
        last_error: ProviderError | None = None
        for api_base, sse_base in self._api_hosts():
            envelope = self._post_form(
                api_base, "/media/download",
                {"request": resource["resource_content"], "no_encrypt": "1"},
            )
            task_id = (envelope.get("data") or {}).get("task_id") if envelope.get("status") == 1 else None
            if not task_id:
                last_error = ProviderError(
                    "vidssave refused to prepare the download "
                    f"({envelope.get('status_code') or envelope.get('msg') or 'unknown'}).",
                    provider=self.name,
                )
                continue
            try:
                download_link, _size = self._poll_task(sse_base, task_id, progress=progress)
            except ProviderError as exc:
                self._cancel_task(api_base, task_id)
                last_error = exc
                continue
            suffix = ".mp4"
            target = Path(self.settings.tmp_dir) / f"vidssave_{next(_uid)}{suffix}"
            self._download_to_file(download_link, target, progress=progress)
            return VideoSource(
                path=target,
                segment_start=0.0,  # full muxed file
                title=title,
                duration=ffprobe_duration(target),
                provider=self.name,
                metadata={
                    "quality": resource.get("quality"),
                    "download_url_host": urlsplit(download_link).netloc,
                },
            )
        raise last_error or ProviderError("vidssave download failed.", provider=self.name)
