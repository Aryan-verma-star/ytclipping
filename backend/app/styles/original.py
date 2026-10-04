"""The default "Original" style — 9:16 Reels/Shorts framing (user decision).

Output is always 1080×1920 (9:16). The source video is scaled to fit and
CENTERED in the vertical frame; the space above/below (for a landscape source)
is filled by the `background` parameter:

- "blur"  — a zoomed, heavily blurred copy of the video itself (the classic
            Instagram Reels / YouTube Shorts look);
- "black" — plain black bars (letterbox).

Re-encoding is mandatory: composing the 9:16 frame cannot be stream-copied.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.ffmpeg import run_ffmpeg
from app.styles.base import ClipStyle, StyleParameter

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920


def _encode_args() -> list[str]:
    # Free-tier reality (512 MB / ~0.1 CPU): unbounded x264 threads spawn one
    # buffer set per reported core (the container reports many more cores than
    # the quota allows) — that thrashes the scheduler AND can OOM the service
    # mid-clip (observed: a 60 fps source killed the container 40 s into a
    # 5 s clip). Two threads encode comfortably within the quota and memory.
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-threads",
        "2",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
    ]


class OriginalStyle(ClipStyle):
    id = "original"
    name = "Original"
    description = (
        "9:16 vertical frame (1080×1920) for Reels/Shorts with the source "
        "video centered."
    )
    parameters = [
        StyleParameter(
            name="background",
            type="enum",
            default="blur",
            choices=["blur", "black"],
            description=(
                "Fill around the centered video: 'blur' uses a zoomed, "
                "blurred copy of the video (reels look); 'black' uses plain "
                "black bars."
            ),
        )
    ]

    def apply(
        self,
        source: Path,
        output: Path,
        *,
        start_in_source: float,
        duration: float,
        params: dict[str, Any],
    ) -> None:
        background = str(params.get("background", "blur") or "blur")
        seek = [
            "-ss",
            f"{max(0.0, start_in_source):.3f}",
            "-i",
            str(source),
            "-t",
            f"{duration:.3f}",
        ]
        w, h = OUTPUT_WIDTH, OUTPUT_HEIGHT
        # 60 fps sources are decimated to 30 fps at the head of the graph:
        # Reels/Shorts standard, halves filter + encode work and buffer
        # pressure on the throttled free-tier CPU.
        if background == "black":
            # Scale to fit inside the 9:16 frame, then pad the remainder black.
            vf = (
                "fps=30,"
                f"scale={w}:{h}:force_original_aspect_ratio=decrease:"
                f"force_divisible_by=2,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,"
                "setsar=1,format=yuv420p"
            )
            run_ffmpeg([*seek, "-vf", vf, *_encode_args(), str(output)])
        else:
            # Split the stream: one branch becomes the blurred backdrop, the
            # other fits inside the frame (foreground); overlay centers it.
            # The backdrop is blurred at 1/5 resolution then upscaled — for a
            # blurred background this is visually identical and much cheaper
            # than a full-resolution gblur (free tiers have ~0.1 CPU).
            fc = (
                "[0:v]fps=30,split=2[bg][fg];"
                f"[bg]scale={w // 5}:{h // 5}:force_original_aspect_ratio=increase,"
                f"crop={w // 5}:{h // 5},setsar=1,gblur=sigma=5,"
                f"scale={w}:{h},setsar=1[bgb];"
                f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:"
                f"force_divisible_by=2,setsar=1[fgc];"
                f"[bgb][fgc]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]"
            )
            run_ffmpeg(
                [
                    *seek,
                    "-filter_complex",
                    fc,
                    "-filter_complex_threads",
                    "2",
                    "-map",
                    "[vout]",
                    "-map",
                    "0:a?",
                    *_encode_args(),
                    str(output),
                ]
            )


STYLE = OriginalStyle()
