"""Validation tests: URL formats, time parsing, window rules (spec §3 Phase 1)."""

from __future__ import annotations

import pytest

from app.core.errors import ValidationAppError
from app.core.validation import (
    extract_youtube_id,
    format_timecode,
    parse_time,
    validate_time_window,
)


# ---------------- URL extraction ----------------

@pytest.mark.parametrize(
    "url,expected_id",
    [
        ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("http://youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=aBcDeFgHiJk&t=30s", "aBcDeFgHiJk"),
        ("https://music.youtube.com/watch?v=abcdefghijk", "abcdefghijk"),
        ("https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PLxyz", "jNQXAC9IVRw"),
        ("https://youtu.be/jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://youtu.be/jNQXAC9IVRw?t=42", "jNQXAC9IVRw"),
        ("youtu.be/jNQXAC9IVRw", "jNQXAC9IVRw"),  # scheme-less input
        ("https://www.youtube.com/shorts/aBcDeFgHiJk", "aBcDeFgHiJk"),
        ("https://www.youtube.com/embed/aBcDeFgHiJk", "aBcDeFgHiJk"),
        ("https://www.youtube.com/live/aBcDeFgHiJk", "aBcDeFgHiJk"),
        ("https://www.youtube.com/v/aBcDeFgHiJk", "aBcDeFgHiJk"),
        ("https://www.youtube-nocookie.com/embed/aBcDeFgHiJk", "aBcDeFgHiJk"),
        ("HTTPS://YOUTU.BE/aBcDeFgHiJk", "aBcDeFgHiJk"),
        ("https://youtu.be/aBcDeFgHiJk/", "aBcDeFgHiJk"),
    ],
)
def test_extract_valid_urls(url, expected_id):
    canonical, video_id = extract_youtube_id(url)
    assert video_id == expected_id
    assert canonical == f"https://www.youtube.com/watch?v={expected_id}"


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/12345",
        "not a url at all",
        "https://youtube.com/playlist?list=PLxyz123",
        "https://youtube.com/watch",
        "https://youtube.com/watch?v=",
        "https://youtube.com/watch?v=tooshort",
        "https://youtube.com/watch?v=waytoolongid123456",
        "https://youtu.be/",
        "https://youtu.be/short",
        "ftp://youtube.com/watch?v=jNQXAC9IVRw",
        "",
        "   ",
        "https://example.com/watch?v=jNQXAC9IVRw",
        "x" * 3000,
    ],
)
def test_extract_rejects_invalid_urls(url):
    with pytest.raises(ValidationAppError):
        extract_youtube_id(url)


def test_extract_rejects_non_string():
    with pytest.raises(ValidationAppError):
        extract_youtube_id(12345)  # type: ignore[arg-type]


# ---------------- time parsing / formatting ----------------

@pytest.mark.parametrize(
    "value,expected",
    [
        (90, 90.0),
        (90.5, 90.5),
        ("90", 90.0),
        ("90.25", 90.25),
        ("1:30", 90.0),
        ("01:02:03", 3723.0),
        ("0:00:10.5", 10.5),
        ("00:10", 10.0),
        ("1:00:00", 3600.0),
        ("  2:05  ", 125.0),
    ],
)
def test_parse_time_valid(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "-5",
        -5,
        -0.1,
        "-1:00",
        "10:70",
        "1:2:3:4",
        "",
        "   ",
        "abc",
        "12:34:56.78.9",
        True,
        None,
    ],
)
def test_parse_time_invalid(value):
    with pytest.raises(ValidationAppError):
        parse_time(value)


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "00:00:00"),
        (59.9, "00:01:00"),  # rounds to 60 seconds
        (60, "00:01:00"),
        (3723, "01:02:03"),
        (600, "00:10:00"),
        (36000, "10:00:00"),
    ],
)
def test_format_timecode(seconds, expected):
    assert format_timecode(seconds) == expected


# ---------------- window validation ----------------

def test_window_ok():
    validate_time_window(10, 70, max_clip_seconds=600, max_source_seconds=14400)


def test_window_start_must_precede_end():
    with pytest.raises(ValidationAppError):
        validate_time_window(70, 70, max_clip_seconds=600, max_source_seconds=14400)
    with pytest.raises(ValidationAppError):
        validate_time_window(71, 70, max_clip_seconds=600, max_source_seconds=14400)


def test_window_clip_length_cap():
    with pytest.raises(ValidationAppError) as exc:
        validate_time_window(0, 601, max_clip_seconds=600, max_source_seconds=14400)
    assert "maximum" in str(exc.value)


def test_window_source_length_cap():
    # 600s window (within the clip cap) ending beyond the source cap
    with pytest.raises(ValidationAppError) as exc:
        validate_time_window(13801, 14401, max_clip_seconds=600, max_source_seconds=14400)
    assert "source" in str(exc.value).lower()
