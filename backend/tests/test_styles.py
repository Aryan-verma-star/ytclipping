"""Clip style registry tests (spec §6, success criterion §12).

Proves: adding a style = one module + registration, with zero changes to the
API, database, or UI logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import ValidationAppError
from app.core.ffmpeg import FFmpegError, ffprobe_video_info, run_ffmpeg
from app.styles import base as styles_base
from app.styles.base import (
    ClipStyle,
    StyleParameter,
    all_styles,
    get_style,
    public_style,
    register_style,
    validate_params,
)


def test_original_style_is_registered():
    style = get_style("original")
    assert style is not None
    assert style.name == "Original"
    assert style.parameters[0].name == "background"


def test_registry_lists_original_with_parameter_metadata():
    public = [public_style(s) for s in all_styles()]
    original = next(s for s in public if s["id"] == "original")
    assert original["description"]
    assert original["parameters"][0]["type"] == "enum"
    assert original["parameters"][0]["default"] == "blur"
    assert original["parameters"][0]["choices"] == ["blur", "black"]


def test_adding_a_new_style_requires_only_registration():
    """The spec's extensibility promise, as an executable check."""

    class DummyStyle(ClipStyle):
        id = "dummy_test_style"
        name = "Dummy"
        description = "Test-only style."
        parameters = [
            StyleParameter(name="zoom", type="number", default=1.0, description="zoom factor"),
            StyleParameter(
                name="gravity",
                type="enum",
                default="center",
                choices=["center", "top"],
                description="anchor",
            ),
        ]

        def apply(self, source, output, *, start_in_source, duration, params):
            raise NotImplementedError

    try:
        register_style(DummyStyle())
        assert get_style("dummy_test_style") is not None
        assert any(s.id == "dummy_test_style" for s in all_styles())
        # parameter validation comes for free from the declarations
        params = validate_params(get_style("dummy_test_style"), {"zoom": "2.5"})
        assert params == {"zoom": 2.5, "gravity": "center"}
        with pytest.raises(ValidationAppError):
            validate_params(get_style("dummy_test_style"), {"gravity": "diagonal"})
        with pytest.raises(ValidationAppError):
            validate_params(get_style("dummy_test_style"), {"unknown_param": 1})
    finally:
        styles_base._REGISTRY.pop("dummy_test_style", None)


def test_duplicate_style_id_rejected():
    class TwinOriginal(ClipStyle):
        id = "original"
        name = "Twin"
        description = "duplicate id"

    with pytest.raises(ValueError):
        register_style(TwinOriginal())


# ---------------- Original style: actual ffmpeg trimming ----------------

@pytest.fixture(scope="module")
def synthetic_source(tmp_path_factory) -> Path:
    """A real 12-second 320x180 video, encoded once for this module."""
    path = tmp_path_factory.mktemp("style_src") / "source.mp4"
    run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "12",
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


def test_original_blur_background_is_916_and_centered(synthetic_source, tmp_path):
    """User decision: clips are 9:16 (1080×1920) with the video centered."""
    style = get_style("original")
    out = tmp_path / "out_blur.mp4"
    style.apply(
        synthetic_source,
        out,
        start_in_source=2.0,
        duration=5.0,
        params={"background": "blur"},
    )
    assert out.exists() and out.stat().st_size > 0
    duration, width, height = ffprobe_video_info(out)
    assert duration is not None
    assert 4.7 <= duration <= 5.3
    assert (width, height) == (1080, 1920)


def test_original_black_background_is_916_and_centered(synthetic_source, tmp_path):
    style = get_style("original")
    out = tmp_path / "out_black.mp4"
    style.apply(
        synthetic_source,
        out,
        start_in_source=2.0,
        duration=5.0,
        params={"background": "black"},
    )
    assert out.exists() and out.stat().st_size > 0
    duration, width, height = ffprobe_video_info(out)
    assert duration is not None
    assert 4.7 <= duration <= 5.3
    assert (width, height) == (1080, 1920)


def test_original_centered_video_content_is_undistorted(synthetic_source, tmp_path):
    """Centered foreground keeps the source aspect; the bars are actually black.

    Composes one clip with background=black, extracts a raw grayscale frame,
    and checks pixel luma: the letterbox bars are near-black while the center
    band (exactly a 16:9 region inside the 9:16 frame) carries video content.
    """
    import subprocess

    style = get_style("original")
    out = tmp_path / "out_frame.mp4"
    style.apply(
        synthetic_source,
        out,
        start_in_source=2.0,
        duration=1.0,
        params={"background": "black"},
    )
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", "0.5", "-i", str(out),
            "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo", "-",
        ],
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    W, H = 1080, 1920
    frame = proc.stdout
    assert len(frame) >= W * H

    def region_mean(x0: int, y0: int, x1: int, y1: int) -> float:
        total = count = 0
        for y in range(y0, y1, 4):
            base = y * W
            row = frame[base + x0: base + x1]
            total += sum(row)
            count += len(row)
        return total / max(1, count)

    # 16:9 source fits the 1080-wide frame as a 1080x606 band, centered
    band_h = 606
    band_top = (H - band_h) // 2
    top_bar = region_mean(0, 0, W, band_top - 16)
    bottom_bar = region_mean(0, band_top + band_h + 16, W, H)
    content = region_mean(0, band_top + 16, W, band_top + band_h - 16)
    assert top_bar < 8.0, f"top bar is not black (mean luma {top_bar:.1f})"
    assert bottom_bar < 8.0, f"bottom bar is not black (mean luma {bottom_bar:.1f})"
    assert content > 10.0, f"center band carries no video content (mean luma {content:.1f})"


def test_original_style_raises_on_missing_source(tmp_path):
    style = get_style("original")
    out = tmp_path / "never.mp4"
    with pytest.raises(FFmpegError):
        style.apply(
            tmp_path / "does_not_exist.mp4",
            out,
            start_in_source=0.0,
            duration=1.0,
            params={},
        )
    assert not out.exists()


def test_original_style_params_validation():
    style = get_style("original")
    assert validate_params(style, None) == {"background": "blur"}
    assert validate_params(style, {"background": "black"}) == {"background": "black"}
    with pytest.raises(ValidationAppError):
        validate_params(style, {"background": "polka-dots"})
    # the old fast_copy parameter is gone — legacy clients get a clear 422
    with pytest.raises(ValidationAppError):
        validate_params(style, {"fast_copy": True})
