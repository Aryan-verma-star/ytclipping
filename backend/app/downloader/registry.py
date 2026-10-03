"""Provider registry — builds the configured chain (spec §5).

DOWNLOADER_PROVIDERS is an ordered, comma-separated chain. Each provider is
tried in order until one succeeds; every failure is captured and reported on
the job. Swapping or adding providers = configuration only, no code changes
elsewhere.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.downloader.base import DownloaderProvider
from app.downloader.cobalt import CobaltProvider
from app.downloader.sample import SampleProvider
from app.downloader.ytdlp import YtDlpProvider

log = logging.getLogger("clipper.downloader")

PROVIDERS: dict[str, type[DownloaderProvider]] = {
    "cobalt": CobaltProvider,
    "ytdlp": YtDlpProvider,
    "sample": SampleProvider,
}


def build_provider_chain(settings: Settings) -> list[DownloaderProvider]:
    chain: list[DownloaderProvider] = []
    for name in settings.provider_chain:
        cls = PROVIDERS.get(name)
        if cls is None:
            raise ValueError(
                f"Unknown downloader provider {name!r}. "
                f"Known providers: {', '.join(sorted(PROVIDERS))}."
            )
        if name == "sample" and settings.environment == "production":
            log.warning(
                "The 'sample' (synthetic dev/test) provider is enabled in production — "
                "it returns synthetic videos, never real downloads."
            )
        chain.append(cls(settings))
    if not chain:
        raise ValueError("DOWNLOADER_PROVIDERS resolved to an empty chain.")
    return chain
