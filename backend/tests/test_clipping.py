"""Clipping-step tests: the ffmpeg trim actually trims correctly."""

from __future__ import annotations

import pytest

from app.core.ffmpeg import FFmpegError, ffprobe_duration, run_ffmpeg


@pytest.fixture(scope="module")
def twelve_second_video(tmp_path_factory):
    path = tmp_path_factory.mktemp("clip") / "input.mp4"
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:sample_rate=44100",
            "-t",
            "12",
            "-g",  # keyframe every 30 frames → bounds stream-copy snapping
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-shortest",
            str(path),
        ]
    )
    return path


def test_reencode_trim_duration_accuracy(twelve_second_video, tmp_path):
    out = tmp_path / "trim.mp4"
    run_ffmpeg(
        [
            "-ss",
            "3.0",
            "-i",
            str(twelve_second_video),
            "-t",
            "5.0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    duration = ffprobe_duration(out)
    assert duration is not None
    assert 4.7 <= duration <= 5.3, f"expected ~5s, got {duration}"


def test_stream_copy_trim_fast_but_keyframe_fuzzy(twelve_second_video, tmp_path):
    out = tmp_path / "trim_fast.mp4"
    run_ffmpeg(
        [
            "-ss",
            "3.0",
            "-i",
            str(twelve_second_video),
            "-t",
            "5.0",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    assert out.stat().st_size > 0
    duration = ffprobe_duration(out)
    assert duration is not None
    # GOP is 1s in the fixture → stream-copy snapping stays within ~±1s
    assert 4.0 <= duration <= 6.0


def test_ffmpeg_error_carries_stderr_tail():
    with pytest.raises(FFmpegError) as exc:
        run_ffmpeg(["-i", "/nonexistent/file.mp4", "/tmp/never.mp4"])
    assert "exited with code" in str(exc.value)


def test_ffprobe_duration_on_garbage_returns_none(tmp_path):
    bad = tmp_path / "garbage.mp4"
    bad.write_bytes(b"this is not a video file" * 100)
    assert ffprobe_duration(bad) is None
