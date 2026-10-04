"""Thin, well-logged wrappers around the ffmpeg/ffprobe binaries."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

log = logging.getLogger("clipper.ffmpeg")

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")

_STDERR_TAIL_CHARS = 2000

# Resolved once: ["nice", "-n", "10"] when coreutils' nice exists, else [].
# Background transcodes/thumbnail seeks must never starve the web process on
# tiny CPU quotas: on Render's free tier a foreground ffmpeg saturating the
# throttled CPU kept uvicorn from answering within the edge's timeout window
# and the site 502'd for minutes while a clip rendered. Deprioritizing ffmpeg
# lets status polls stay fast; renders take marginally longer but the UI
# stays alive. (Applied via an exec wrapper — no preexec_fn fork-side risk.)
_NICE_PREFIX: list[str] | None = None


def nice_prefix() -> list[str]:
    global _NICE_PREFIX
    if _NICE_PREFIX is None:
        _NICE_PREFIX = ["nice", "-n", "10"] if shutil.which("nice") else []
    return _NICE_PREFIX


class FFmpegError(RuntimeError):
    """ffmpeg/ffprobe failed; the message carries the stderr tail."""


def _tail(text: str, limit: int = _STDERR_TAIL_CHARS) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def binaries_available() -> bool:
    return bool(shutil.which(FFMPEG_BIN)) and bool(shutil.which(FFPROBE_BIN))


def ffmpeg_version() -> str | None:
    try:
        proc = subprocess.run(
            [FFMPEG_BIN, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0:
            return proc.stdout.splitlines()[0] if proc.stdout else "unknown"
    except Exception:  # pragma: no cover - diagnostic path only
        pass
    return None


def run_ffmpeg(args: list[str], *, timeout: float = 3600.0) -> None:
    """Run ffmpeg; raise FFmpegError with the stderr tail on failure.

    ``-nostdin`` is mandatory: with an inherited open stdin (docker run -i,
    a terminal, some process supervisors) ffmpeg switches to an interactive
    command prompt after the work is done and the call never returns.
    """
    cmd = [
        *nice_prefix(),
        FFMPEG_BIN,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        *args,
    ]
    log.debug("ffmpeg: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"ffmpeg timed out after {int(timeout)}s") from exc
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg exited with code {proc.returncode}: {_tail(proc.stderr)}")


def ffprobe_json(path: os.PathLike | str) -> dict:
    cmd = [
        *nice_prefix(),
        FFPROBE_BIN,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError("ffprobe timed out") from exc
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed: {_tail(proc.stderr)}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegError("ffprobe returned non-JSON output") from exc


def ffprobe_duration(path: os.PathLike | str) -> float | None:
    """Duration of the file in seconds, or None when undeterminable."""
    try:
        info = ffprobe_json(path)
    except FFmpegError:
        return None
    fmt = info.get("format") or {}
    try:
        return float(fmt.get("duration"))
    except (TypeError, ValueError):
        pass
    for stream in info.get("streams") or []:
        if stream.get("duration"):
            try:
                return float(stream["duration"])
            except (TypeError, ValueError):
                continue
    return None


def ffprobe_remote_info(
    url: str,
    headers: dict | None = None,
    *,
    timeout: float = 30.0,
) -> tuple[float | None, int | None, int | None]:
    """(duration, width, height) probed over HTTP(S) without a full download.

    ffprobe range-requests just enough of the remote media to read its
    container headers, which makes this cheap enough for the instant-playback
    resolve step. Any failure → (None, None, None) — callers decide whether
    that is fatal (for the preview timeline the browser can still supply the
    duration once playback starts).
    """
    cmd = [
        *nice_prefix(),
        FFPROBE_BIN,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
    ]
    if headers:
        # ffprobe takes HTTP headers as one CRLF-joined blob via -headers.
        blob = "\r\n".join(f"{k}: {v}" for k, v in headers.items() if str(k).strip())
        if blob:
            cmd += ["-headers", blob]
    cmd.append(str(url))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None, None, None
    if proc.returncode != 0:
        log.debug("ffprobe remote failed for %s: %s", url, _tail(proc.stderr, 300))
        return None, None, None
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, None, None
    return _extract_video_info(info)


def _extract_video_info(info: dict) -> tuple[float | None, int | None, int | None]:
    """Shared JSON parsing for ffprobe_video_info / ffprobe_remote_info."""
    duration = None
    fmt = info.get("format") or {}
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        duration = None
    width = height = None
    for stream in info.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        try:
            width = int(stream["width"])
            height = int(stream["height"])
        except (KeyError, TypeError, ValueError):
            pass
        if duration is None and stream.get("duration"):
            try:
                duration = float(stream["duration"])
            except (TypeError, ValueError):
                pass
        break
    return duration, width, height


def ffprobe_video_info(path: os.PathLike | str) -> tuple[float | None, int | None, int | None]:
    """(duration_seconds, width, height) of the first video stream.

    Any value the probe cannot determine comes back as None. Used by the
    preview pipeline (timeline metadata) and tests.
    """
    try:
        info = ffprobe_json(path)
    except FFmpegError:
        return None, None, None
    return _extract_video_info(info)
