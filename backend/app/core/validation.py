"""Input validation (spec §3 Phase 1).

- Valid YouTube URL formats: watch, youtu.be, shorts, embed, live.
- Start time earlier than end time.
- Times within the configured source-duration cap (actual video duration is
  additionally enforced during processing, when the provider reports it).
- Configurable maximum clip length (MAX_CLIP_SECONDS).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from app.core.errors import ValidationAppError

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "gaming.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}

_PATH_ID_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/", "/e/")

MAX_URL_LENGTH = 2048


def _fail(message: str, field: str = "url") -> None:
    raise ValidationAppError(message, field=field)


def extract_youtube_id(raw_url: str) -> tuple[str, str]:
    """Return (canonical_url, video_id) or raise ValidationAppError.

    Accepts: youtube.com/watch?v=ID, youtu.be/ID, /shorts/ID, /embed/ID,
    /live/ID, /v/ID, /e/ID — with or without scheme, any of the hosts above.
    Rejects playlists without a video ID, malformed IDs, and non-YouTube hosts.
    """
    if not isinstance(raw_url, str) or not raw_url.strip():
        _fail("A YouTube URL is required.")
    url = raw_url.strip()
    if len(url) > MAX_URL_LENGTH:
        _fail("URL is too long.")
    if "://" not in url:
        url = "https://" + url

    try:
        parts = urlsplit(url)
    except ValueError:
        _fail("Could not parse the URL.")

    if parts.scheme not in ("http", "https"):
        _fail("URL scheme must be http or https.")

    host = (parts.hostname or "").lower()
    path = parts.path or "/"

    video_id: str | None = None

    if host in _SHORT_HOSTS:
        segments = [s for s in path.split("/") if s]
        if segments:
            video_id = segments[0]
    elif host in _YOUTUBE_HOSTS:
        if path == "/watch":
            query = parse_qs(parts.query)
            candidates = query.get("v") or query.get("video_id") or []
            if candidates:
                video_id = candidates[0]
        else:
            for prefix in _PATH_ID_PREFIXES:
                if path.startswith(prefix):
                    rest = path[len(prefix):]
                    video_id = rest.split("/")[0].split("?")[0]
                    break

    if not video_id:
        _fail(
            "Not a recognizable single-video YouTube URL. "
            "Supported formats: youtube.com/watch?v=…, youtu.be/…, "
            "youtube.com/shorts/…, /embed/…, /live/…"
        )

    if not _VIDEO_ID_RE.match(video_id):
        _fail(f"Extracted video ID {video_id!r} is not a valid 11-character YouTube ID.")

    canonical = f"https://www.youtube.com/watch?v={video_id}"
    return canonical, video_id


def parse_time(value: object, field: str = "start_time") -> float:
    """Accept seconds as number, or 'SS', 'MM:SS', 'HH:MM:SS' strings."""
    from app.core.errors import ValidationAppError as VErr

    if isinstance(value, bool):
        raise VErr("Time must be a number or HH:MM:SS string.", field=field)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            raise VErr("Time cannot be negative.", field=field)
        return seconds
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise VErr("Time is required (seconds or HH:MM:SS).", field=field)
        pieces = text.split(":")
        if len(pieces) > 3:
            raise VErr(f"{text!r} is not a valid time — use seconds or HH:MM:SS.", field=field)
        try:
            numbers = [float(p) for p in pieces]
        except ValueError:
            raise VErr(f"{text!r} is not a valid time — use seconds or HH:MM:SS.", field=field)
        if any(n < 0 for n in numbers):
            raise VErr("Time cannot be negative.", field=field)
        if len(numbers) == 3:
            hours, minutes, seconds = numbers
        elif len(numbers) == 2:
            hours, (minutes, seconds) = 0.0, numbers
        else:
            # a bare number is total seconds — may exceed 59
            return numbers[0]
        if minutes >= 60 or seconds >= 60:
            raise VErr("Minutes and seconds must be below 60.", field=field)
        return hours * 3600.0 + minutes * 60.0 + seconds
    raise VErr("Time must be a number or HH:MM:SS string.", field=field)


def format_timecode(seconds: float) -> str:
    """Render seconds as HH:MM:SS (spec §7: stored as seconds, displayed as HH:MM:SS)."""
    total = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def validate_time_window(
    start: float,
    end: float,
    *,
    max_clip_seconds: float,
    max_source_seconds: float,
) -> None:
    """Cross-field validation of the start/end window."""
    if start >= end:
        raise ValidationAppError(
            "Start time must be earlier than end time.",
            field="start_time",
        )
    duration = end - start
    if duration > max_clip_seconds:
        raise ValidationAppError(
            f"Clip length is {int(duration)}s but the maximum allowed is "
            f"{int(max_clip_seconds)}s.",
            field="end_time",
        )
    if end > max_source_seconds:
        raise ValidationAppError(
            f"End time exceeds the maximum source length of "
            f"{format_timecode(max_source_seconds)}.",
            field="end_time",
        )
