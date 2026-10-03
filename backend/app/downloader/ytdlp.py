"""yt-dlp fallback provider (locally-run downloader tool, spec §5).

Runs the bundled yt-dlp binary. Key properties:
- Downloads ONLY the requested segment via --download-sections (bandwidth and
  free-tier friendly), with --force-keyframes-at-cuts for accurate boundaries.
- Caps resolution at MAX_VIDEO_HEIGHT (lowest sufficient quality policy).
- Optional --cookies file (YTDLP_COOKIES_FILE): the standard remedy when
  YouTube bot-checks datacenter IPs (verified on 2026-10-03: cloud IPs get
  "Sign in to confirm you're not a bot" without cookies).
- Segment downloads are re-encoded at the cut points; segment_start is
  reported as the requested start (documented assumption).

Instant-load: ``resolve_stream`` runs a metadata-only (-J, --simulate) call
selecting a MUXED (video+audio) progressive format — its direct URL plus the
title/duration let the preview UI play instantly through the backend proxy
while get_video() fetches the best-quality file in the background.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.core.ffmpeg import ffprobe_duration
from app.downloader.base import (
    DownloaderProvider,
    ProgressCB,
    ProviderError,
    StreamTarget,
    VideoSource,
)

log = logging.getLogger("clipper.ytdlp")

_uid = itertools.count(1)

_VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".m4v", ".mov"}

# muxed formats only — a video-only stream would play silently in the preview
_MUXED_FORMAT = "b[vcodec!=none][acodec!=none]/b"

_PROGRESS_RE = re.compile(r"^\[download\]\s+(\d+(?:\.\d+)?)%")
_RESOLVE_TIMEOUT_SECONDS = 45.0


class YtDlpProvider(DownloaderProvider):
    name = "ytdlp"

    def __init__(self, settings) -> None:
        self.settings = settings
        self._cookies_arg = self._resolve_cookies(settings)

    @staticmethod
    def _resolve_cookies(settings) -> str:
        """Cookies come from either a file path or raw env-var content.

        CLIPPER_YTDLP_COOKIES (content) is written to a 0600 file under the
        data dir once at startup — the practical option on hosts where
        uploading a file next to the code is awkward (Render, Fly, etc.).
        A file path, when given, always wins.
        """
        if settings.ytdlp_cookies_file:
            return settings.ytdlp_cookies_file
        content = (settings.ytdlp_cookies or "").strip()
        if not content:
            return ""
        try:
            cookies_dir = settings.resolved_data_dir
            cookies_dir.mkdir(parents=True, exist_ok=True)
            cookies_path = cookies_dir / "cookies-from-env.txt"
            cookies_path.write_text(content + "\n", encoding="utf-8")
            cookies_path.chmod(0o600)
            log.info("yt-dlp cookies: wrote %d bytes from env to %s", len(content), cookies_path)
            return str(cookies_path)
        except OSError as exc:  # unwritable data dir — degrade to no cookies
            log.warning("could not materialize cookies from env (%s)", exc)
            return ""

    def _base_cmd(self) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--no-warnings",
        ]
        if self._cookies_arg:
            cmd += ["--cookies", self._cookies_arg]
        if self.settings.ytdlp_extra_args:
            cmd += self.settings.ytdlp_extra_args.split()
        return cmd

    def build_command(self, url: str, start: float, end: float, target_dir: Path) -> list[str]:
        height = int(self.settings.max_video_height)
        return [
            *self._base_cmd(),
            "--no-progress",
            "--print",
            "json",
            "--no-simulate",
            "--download-sections",
            f"*{start:.3f}-{end:.3f}",
            "--force-keyframes-at-cuts",
            "-f",
            f"bv*[height<={height}]+ba/b[height<={height}]/b",
            "--merge-output-format",
            "mp4",
            "-o",
            str(target_dir / "source.%(ext)s"),
            url,
        ]

    # -- instant playback resolution --------------------------------------

    def resolve_stream(self, url: str, start: float, end: float) -> StreamTarget | None:
        cmd = [
            *self._base_cmd(),
            "--simulate",
            "-J",
            "-f",
            _MUXED_FORMAT,
            url,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_RESOLVE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            log.info("yt-dlp resolve_stream timed out for %s", url)
            return None
        if proc.returncode != 0:
            log.info(
                "yt-dlp resolve_stream failed (falling back to plain download): %s",
                self._friendly_error(proc.stderr)[:200],
            )
            return None
        info = self._parse_info(proc.stdout)
        if not info or not info.get("url"):
            return None
        # the fallback "b" may still be video-only → preview would be silent
        if info.get("vcodec") in (None, "none") or info.get("acodec") in (None, "none"):
            log.info("yt-dlp resolve_stream: no muxed format available for %s", url)
            return None
        duration = info.get("duration")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        headers = {
            k: str(v)
            for k, v in (info.get("http_headers") or {}).items()
            if isinstance(k, str) and isinstance(v, str)
        }
        return StreamTarget(
            provider=self.name,
            url=str(info["url"]),
            headers=headers,
            title=info.get("title"),
            duration=duration,
        )

    # -- download -----------------------------------------------------------

    def get_video(
        self,
        url: str,
        start: float,
        end: float,
        progress: ProgressCB | None = None,
    ) -> VideoSource:
        target_dir = Path(self.settings.tmp_dir) / f"ytdlp_{next(_uid)}"
        target_dir.mkdir(parents=True, exist_ok=True)
        cmd = self.build_command(url, start, end, target_dir)
        log.info("yt-dlp fetching %s [%s-%s]", url, start, end)

        if progress is None:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.settings.ytdlp_timeout_seconds,
            )
            stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
        else:
            stdout, stderr, returncode = self._run_with_progress(cmd, progress)

        if returncode != 0:
            raise ProviderError(
                f"yt-dlp failed: {self._friendly_error(stderr)}",
                provider=self.name,
            )

        info = self._parse_info(stdout)
        files = [p for p in sorted(target_dir.iterdir()) if p.suffix.lower() in _VIDEO_EXTS]
        if not files:
            raise ProviderError(
                "yt-dlp reported success but no video file was found — "
                "the tool's output behavior may have changed.",
                provider=self.name,
            )
        source_file = files[0]
        return VideoSource(
            path=source_file,
            # Sections are cut at (re-encoded) keyframes at the requested bounds.
            segment_start=float(start),
            title=(info or {}).get("title"),
            duration=ffprobe_duration(source_file),
            provider=self.name,
            metadata={"info": {"id": (info or {}).get("id")} if info else {}},
        )

    def _run_with_progress(
        self, cmd: list[str], progress: ProgressCB
    ) -> tuple[str, str, int]:
        """Popen variant that streams [download] x.y% lines as they happen.

        subprocess.run would buffer stdout until exit — useless for progress.
        stderr is drained on a side thread so the pipe can never fill up and
        deadlock the child (yt-dlp writes errors/warnings there).
        """
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stderr_chunks: list[str] = []
        drain = threading.Thread(
            target=lambda: stderr_chunks.append(proc.stderr.read() if proc.stderr else ""),
            daemon=True,
        )
        drain.start()

        stdout_lines: list[str] = []
        last_report = 0.0
        deadline = time.monotonic() + self.settings.ytdlp_timeout_seconds
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                stdout_lines.append(line)
                match = _PROGRESS_RE.match(line.strip())
                if match and time.monotonic() - last_report >= 0.5:
                    last_report = time.monotonic()
                    try:
                        progress(min(float(match.group(1)) / 100.0, 1.0))
                    except Exception:  # pragma: no cover - a broken UI callback
                        log.debug("progress callback raised", exc_info=True)
                if time.monotonic() > deadline:
                    proc.kill()
                    raise ProviderError(
                        f"yt-dlp timed out after {int(self.settings.ytdlp_timeout_seconds)}s.",
                        provider=self.name,
                    )
            returncode = proc.wait(timeout=max(30.0, self.settings.ytdlp_timeout_seconds))
        except ProviderError:
            raise
        except Exception as exc:
            proc.kill()
            raise ProviderError(f"yt-dlp run failed: {exc}", provider=self.name) from exc
        finally:
            drain.join(timeout=5.0)
        try:
            progress(1.0)
        except Exception:  # pragma: no cover
            pass
        return "".join(stdout_lines), "".join(stderr_chunks), returncode

    def _parse_info(self, stdout: str) -> dict | None:
        for line in reversed((stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None

    @staticmethod
    def _friendly_error(stderr: str) -> str:
        tail = (stderr or "").strip()[-500:]
        if "Sign in to confirm" in tail or "not a bot" in tail:
            return (
                "YouTube is bot-checking this server's IP. Configure "
                "CLIPPER_YTDLP_COOKIES (cookies.txt content) or "
                "CLIPPER_YTDLP_COOKIES_FILE (path), or run from a residential "
                f"IP. Underlying message: {tail[-200:]}"
            )
        if "Video unavailable" in tail:
            return "The video is unavailable (removed, private, or region-locked)."
        if "Private video" in tail:
            return "The video is private."
        if "members-only" in tail.lower():
            return "The video is members-only content."
        return tail or "unknown yt-dlp error"
