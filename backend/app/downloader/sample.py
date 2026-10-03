"""DEVELOPMENT / TEST provider — generates a synthetic local video.

Purpose: verify the whole pipeline (download → trim → serve) without touching
any third-party service. Used by the automated test suite and by the sandbox
demo, where YouTube blocks datacenter IPs and no cobalt key is available.

It is NEVER enabled by accident: it only runs when `sample` is explicitly
listed in DOWNLOADER_PROVIDERS. The generated clip carries a burned-in
timecode so you can visually confirm the trim boundaries are correct.

Instant-load: there is no remote URL to stream, but the duration is known
before synthesis starts, so ``resolve_stream`` returns a URL-less target —
the timeline renders immediately and the player appears once the (few
seconds long) synthesis finishes.
"""

from __future__ import annotations

import itertools
import logging
import subprocess
import time
from pathlib import Path

from app.core.ffmpeg import FFmpegError, ffprobe_duration
from app.downloader.base import (
    DownloaderProvider,
    ProgressCB,
    StreamTarget,
    VideoSource,
)

log = logging.getLogger("clipper.sample")

_uid = itertools.count(1)

_SAMPLE_TITLE = "Sample synthetic video (dev/test provider)"


class SampleProvider(DownloaderProvider):
    name = "sample"  # clearly labeled as the synthetic dev/test provider

    def __init__(self, settings) -> None:
        self.settings = settings

    def _planned_duration(self, end: float) -> float:
        # Simulate a "full video" covering [0, end], capped for practicality.
        # Requests beyond the cap fail the same way they would for a real
        # short video ("start beyond the end of the video").
        return min(max(2.0, float(end) + 2.0), float(self.settings.sample_max_seconds))

    # -- interface --------------------------------------------------------

    def resolve_stream(self, url: str, start: float, end: float) -> StreamTarget | None:
        return StreamTarget(
            provider=self.name,
            url=None,  # nothing to proxy — the file must be synthesized first
            title=_SAMPLE_TITLE,
            duration=self._planned_duration(end),
        )

    def get_video(
        self,
        url: str,
        start: float,
        end: float,
        progress: ProgressCB | None = None,
    ) -> VideoSource:
        duration = self._planned_duration(end)
        target = Path(self.settings.tmp_dir) / f"sample_{next(_uid)}.mp4"
        height = min(int(self.settings.max_video_height), 720)
        width = height * 16 // 9 // 2 * 2  # even dimensions

        base_args = [
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            f"{duration:.3f}",
        ]
        # Burn a timecode so trimming is visually verifiable; fall back to a
        # plain synthesis if no font is available on the host.
        # (Built by concatenation: the %{...} expansion must survive verbatim.)
        size = max(20, height // 18)
        drawtext = (
            "drawtext=text='SAMPLE %{pts\\:hms}':x=24:y=24:fontsize="
            + str(size)
            + ":fontcolor=white:box=1:boxcolor=black@0.6"
        )
        encode_args = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-shortest",
            str(target),
        ]
        try:
            self._run_synth([*base_args, "-vf", drawtext, *encode_args], duration, progress)
        except FFmpegError:
            log.warning("sample provider: drawtext failed (no fonts?), retrying plain synthesis")
            self._run_synth([*base_args, *encode_args], duration, progress)

        return VideoSource(
            path=target,
            segment_start=0.0,
            title=_SAMPLE_TITLE,
            duration=ffprobe_duration(target),
            provider=self.name,
            metadata={"synthetic": True, "requested_url": url},
        )

    def _run_synth(
        self, args: list[str], duration: float, progress: ProgressCB | None
    ) -> None:
        """ffmpeg synthesis with live progress via -progress pipe:1.

        Mirrors core.ffmpeg.run_ffmpeg semantics (-nostdin, loglevel error,
        FFmpegError with the stderr tail) but streams ``out_time_us`` lines so
        the preview UI can show the synthesis filling up in real time.
        """
        if progress is None:
            from app.core.ffmpeg import run_ffmpeg

            run_ffmpeg(args, timeout=600)
            return

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            *args,
            "-progress",
            "pipe:1",
            "-nostats",
        ]
        log.debug("ffmpeg(synth): %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stderr_chunks: list[str] = []
        import threading

        threading.Thread(
            target=lambda: stderr_chunks.append(proc.stderr.read() if proc.stderr else ""),
            daemon=True,
        ).start()

        last_report = 0.0
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    value = line.split("=", 1)[1].strip()
                    if value.isdigit() and time.monotonic() - last_report >= 0.25:
                        last_report = time.monotonic()
                        try:
                            progress(min(int(value) / 1e6 / duration, 1.0))
                        except Exception:  # pragma: no cover
                            pass
            returncode = proc.wait(timeout=120)
        except Exception as exc:
            proc.kill()
            raise FFmpegError(f"ffmpeg synthesis failed: {exc}") from exc
        if returncode != 0:
            raise FFmpegError(
                f"ffmpeg exited with code {returncode}: {''.join(stderr_chunks)[-2000:]}"
            )
        try:
            progress(1.0)
        except Exception:  # pragma: no cover
            pass
