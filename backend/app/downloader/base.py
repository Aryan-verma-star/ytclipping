"""Downloader provider interface (spec §5).

v1.5 "instant load" additions:
- ``StreamTarget`` — everything needed to START PLAYING a video before the
  full-quality file is downloaded (a direct, seekable media URL + metadata).
- ``DownloaderProvider.resolve_stream()`` — optional capability a provider
  implements when it can produce a StreamTarget cheaply (one API call, no
  full download). Providers that cannot just keep the default (None).
- ``progress`` callback on ``get_video`` — the preview pipeline reports
  background-download progress to the UI.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Fraction of the download completed (0..1), or None when indeterminate.
ProgressCB = Callable[[float | None], None]


class ProviderError(Exception):
    """A provider failed in an expected way (timeout, blocked, site changed…).

    `transient` hints the orchestrator may retry later (e.g. rate limits).
    """

    def __init__(self, message: str, *, provider: str, transient: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.transient = transient


@dataclass
class VideoSource:
    """A local file holding (at least) the requested segment of the video."""

    path: Path
    segment_start: float = 0.0
    title: str | None = None
    duration: float | None = None  # duration of the FILE (not the original video)
    provider: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class StreamTarget:
    """A cheaply-resolved handle for instant playback.

    ``url``  — direct, seekable (Range-capable) media URL the backend can
               proxy to the browser while the real download runs. May be None
               when the provider knows the duration but has no remote URL
               (e.g. the synthetic sample provider) — the timeline can still
               render immediately.
    ``headers`` — any headers the upstream requires (User-Agent, Referer…).
    ``duration`` — seconds, when the provider or a cheap remote probe knows it.
    """

    provider: str
    url: str | None = None
    headers: dict = field(default_factory=dict)
    title: str | None = None
    duration: float | None = None

    @property
    def playable(self) -> bool:
        return bool(self.url)


class DownloaderProvider(ABC):
    """Contract every provider implements. Swapping providers touches nothing else."""

    name: str = "abstract"

    # True when get_video() can skip its own resolution step and download a
    # StreamTarget's URL directly (cobalt: same tunnel URL — saves an API call).
    supports_reuse: bool = False

    @abstractmethod
    def get_video(
        self,
        url: str,
        start: float,
        end: float,
        progress: ProgressCB | None = None,
    ) -> VideoSource:
        """Fetch the requested range and return a local VideoSource.

        Implementations should download only the needed segment or the lowest
        sufficient quality where the tooling allows it (spec §5). ``progress``
        is optional; when given it should be called periodically with the
        completed fraction (or None when indeterminate).
        """
        raise NotImplementedError

    def resolve_stream(self, url: str, start: float, end: float) -> StreamTarget | None:
        """Best-effort instant-playback resolution. MUST be fast (metadata only).

        Returns None when the provider cannot resolve a stream — the caller
        falls back to the normal download path. Errors are the caller's to
        tolerate; implementations should prefer returning None over raising
        for expected situations (unconfigured, unsupported site, bot check…).
        """
        return None
