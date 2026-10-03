"""cobalt-compatible third-party download API provider (primary, spec §5).

Talks to any instance implementing the open-source cobalt API schema
(https://github.com/imputnet/cobalt). The instance URL and an optional bearer
key come from COBALT_API_URL / COBALT_API_KEY — nothing is hardcoded, and no
URLs are invented. The official api.cobalt.tools instance [VERIFIED
2026-10-03] now requires JWT auth, so you must bring your own instance or key.

Instant-load: the instance hands us a direct, seekable media URL, so
``resolve_stream`` returns it (plus a cheap remote ffprobe for the duration)
and the preview UI can start playing through the backend proxy while
``get_video`` downloads the very same tunnel URL in the background.

CAUTION (spec §5 documentation requirement): third-party download services
are unofficial and can change or break without notice. This provider is
expected to need maintenance. Failures surface as job errors, never crashes.
"""

from __future__ import annotations

import itertools
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.core.ffmpeg import ffprobe_duration, ffprobe_remote_info
from app.downloader.base import (
    DownloaderProvider,
    ProgressCB,
    ProviderError,
    StreamTarget,
    VideoSource,
)

log = logging.getLogger("clipper.cobalt")

_uid = itertools.count(1)

# cobalt tunnel URLs are plain media files — no special client headers needed,
# but a browser-ish UA avoids the odd CDN that 403s python-httpx's default.
_DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Clipper/1.5"}


class CobaltProvider(DownloaderProvider):
    name = "cobalt"
    supports_reuse = True  # get_video can download a resolved tunnel URL as-is

    def __init__(self, settings) -> None:
        self.settings = settings

    # -- helpers ---------------------------------------------------------
    def _quality(self) -> str:
        height = int(self.settings.max_video_height)
        for bucket in (360, 480, 720, 1080):
            if height <= bucket:
                return str(bucket)
        return "1080"

    def _request_download(self, url: str) -> dict:
        api_url = self.settings.cobalt_api_url.rstrip("/")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.settings.cobalt_api_key:
            headers["Authorization"] = f"Bearer {self.settings.cobalt_api_key}"
        payload = {
            "url": url,
            "videoQuality": self._quality(),
            "downloadMode": "auto",
            "filenameStyle": "basic",
        }
        timeout = httpx.Timeout(
            connect=10.0,
            read=self.settings.cobalt_timeout_seconds,
            write=15.0,
            pool=10.0,
        )
        response = httpx.post(api_url, json=payload, headers=headers, timeout=timeout)
        if response.status_code in (401, 403):
            raise ProviderError(
                "cobalt instance rejected our credentials (check COBALT_API_URL / COBALT_API_KEY).",
                provider=self.name,
            )
        if response.status_code == 429:
            raise ProviderError(
                "cobalt instance rate-limited the request; retry later.",
                provider=self.name,
                transient=True,
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"cobalt API returned HTTP {response.status_code}: {response.text[:200]}",
                provider=self.name,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(
                "cobalt API returned a non-JSON body (instance changed or is behind a captcha).",
                provider=self.name,
            ) from exc

    def _extract_media_url(self, data: dict) -> tuple[str, str | None]:
        """Validate the cobalt response shape; returns (media_url, filename)."""
        status = data.get("status")
        if status == "error":
            err = data.get("error") or {}
            code = err.get("code", "unknown")
            raise ProviderError(
                f"cobalt reported an error for this video: {code}",
                provider=self.name,
            )
        if status == "picker":
            raise ProviderError(
                "cobalt returned multiple files (picker) — not supported for clipping.",
                provider=self.name,
            )
        if status not in ("tunnel", "redirect") or not data.get("url"):
            raise ProviderError(
                f"unexpected cobalt response shape (status={status!r}); instance API may have changed.",
                provider=self.name,
            )
        return data["url"], data.get("filename") or None

    def _stream_to_file(
        self,
        download_url: str,
        target: Path,
        progress: ProgressCB | None = None,
    ) -> None:
        cap = int(self.settings.max_download_bytes)
        # Streaming read timeout: generous between chunks, bounded overall.
        timeout = httpx.Timeout(
            connect=10.0,
            read=120.0,
            write=30.0,
            pool=10.0,
        )
        written = 0
        last_report = 0.0
        try:
            with httpx.stream(
                "GET", download_url, timeout=timeout, follow_redirects=True,
                headers=_DEFAULT_HEADERS,
            ) as resp:
                if resp.status_code >= 400:
                    raise ProviderError(
                        f"downloading from cobalt tunnel failed with HTTP {resp.status_code}.",
                        provider=self.name,
                    )
                total: int | None = None
                if resp.headers.get("content-length", "").isdigit():
                    total = int(resp.headers["content-length"])
                with open(target, "wb") as fh:
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
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"network error while downloading from the cobalt instance: {exc}",
                provider=self.name,
                transient=True,
            ) from exc
        if written == 0:
            raise ProviderError("cobalt tunnel produced an empty file.", provider=self.name)
        if progress is not None:
            progress(1.0)

    # -- interface --------------------------------------------------------
    def resolve_stream(self, url: str, start: float, end: float) -> StreamTarget | None:
        if not self.settings.cobalt_api_url:
            return None  # unconfigured — let the chain report it on the real download
        try:
            data = self._request_download(url)
            media_url, filename = self._extract_media_url(data)
        except ProviderError as exc:
            log.info("cobalt resolve_stream unavailable: %s", exc.message)
            return None
        duration, _w, _h = ffprobe_remote_info(media_url, _DEFAULT_HEADERS)
        if duration is not None and duration <= 0:
            duration = None
        return StreamTarget(
            provider=self.name,
            url=media_url,
            headers=dict(_DEFAULT_HEADERS),
            title=filename,
            duration=duration,
        )

    def get_video(
        self,
        url: str,
        start: float,
        end: float,
        progress: ProgressCB | None = None,
        reuse: StreamTarget | None = None,
    ) -> VideoSource:
        if not self.settings.cobalt_api_url:
            raise ProviderError(
                "cobalt provider is not configured — set COBALT_API_URL "
                "(and COBALT_API_KEY if your instance requires auth).",
                provider=self.name,
            )

        if reuse is not None and reuse.provider == self.name and reuse.url:
            # the resolve step already asked the instance — download that URL
            download_url, title = reuse.url, reuse.title
            log.info("cobalt reusing tunnel URL resolved for instant playback")
        else:
            data = self._request_download(url)
            download_url, title = self._extract_media_url(data)

        suffix = Path(urlsplit(download_url).path).suffix or ".mp4"
        if suffix not in (".mp4", ".webm", ".mkv", ".m4v"):
            suffix = ".mp4"
        target = Path(self.settings.tmp_dir) / f"cobalt_{next(_uid)}{suffix}"
        self._stream_to_file(download_url, target, progress=progress)

        return VideoSource(
            path=target,
            segment_start=0.0,  # cobalt returns the full video
            title=title,
            duration=ffprobe_duration(target),
            provider=self.name,
            metadata={"download_url_host": urlsplit(download_url).netloc},
        )
