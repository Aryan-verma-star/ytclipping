"""Phase 4 extension point — AI clip-segment analysis (spec §3 Phase 4).

NOT IMPLEMENTED BY DESIGN. This module only defines the contract that a
future AI module must satisfy. See docs/ai-integration.md.

The future module (e.g. wrapping the open-source GitHub repository the user
will supply) implements VideoAnalyzer, calls register_analyzer(), and the
suggestions flow straight into the existing job-creation flow:

    analyzer = get_analyzer()                 # configured implementation
    suggestions = analyzer.analyze(url)       # [{start, end, label, reason}]
    for s in suggestions:
        POST /api/jobs {url, start_time: s.start, end_time: s.end, ...}

No changes are required in the job pipeline, persistence, or styles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SuggestedSegment:
    """One AI-suggested clip: a time window plus a human-readable reason."""

    start: float  # seconds
    end: float  # seconds
    label: str  # short title, e.g. "Key moment: the demo"
    reason: str = ""  # why this segment was chosen
    confidence: float | None = None


@runtime_checkable
class VideoAnalyzer(Protocol):
    """Contract for future AI analyzers.

    analyze() accepts a video URL (or a local path, depending on the
    implementation) and returns suggested segments. Implementations must
    validate their own outputs against the app's limits (MAX_CLIP_SECONDS,
    MAX_SOURCE_SECONDS) — the same validation the POST /api/jobs endpoint
    applies.
    """

    id: str
    name: str

    def analyze(self, url: str, **kwargs) -> list[SuggestedSegment]: ...


# --- registry (empty on purpose — no real logic ships in this build) -------
_ANALYZERS: dict[str, VideoAnalyzer] = {}


def register_analyzer(analyzer: VideoAnalyzer) -> VideoAnalyzer:
    _ANALYZERS[analyzer.id] = analyzer
    return analyzer


def get_analyzer(analyzer_id: str | None = None) -> VideoAnalyzer | None:
    if not _ANALYZERS:
        return None
    if analyzer_id:
        return _ANALYZERS.get(analyzer_id)
    return next(iter(_ANALYZERS.values()))


def available_analyzers() -> list[str]:
    return list(_ANALYZERS)
