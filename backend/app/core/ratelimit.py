"""Dependency-free per-IP sliding-window rate limiting (spec §10).

Four buckets:
- general requests per minute (RATE_LIMIT_PER_MINUTE)
- job creations per minute (RATE_LIMIT_JOBS_PER_MINUTE)
- preview creations per minute (RATE_LIMIT_PREVIEWS_PER_MINUTE) — stricter
  because each preview is a full source download
- status polls per minute (RATE_LIMIT_STATUS_PER_MINUTE) — generous; the
  frontend legitimately polls preview/job status ~1x/s for minutes while
  downloads/renders run, and starving those pollers freezes the UI

Client identity comes from X-Real-IP / X-Forwarded-For (set by the Caddy
gateway in the sandbox and by Render's edge in production), falling back to
the socket address. In-memory only: appropriate for a single-instance
personal deployment.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowCounter:
    def __init__(self, max_events: int, window_seconds: float = 60.0) -> None:
        self.max_events = max(1, max_events)
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> None:
        queue = self._events[key]
        horizon = now - self.window_seconds
        while queue and queue[0] < horizon:
            queue.popleft()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        self._prune(key, now)
        queue = self._events[key]
        if len(queue) >= self.max_events:
            return False
        queue.append(now)
        return True

    def retry_after(self, key: str) -> int:
        """Seconds until one event leaves the window (for Retry-After)."""
        now = time.monotonic()
        self._prune(key, now)
        queue = self._events[key]
        if not queue:
            return 1
        wait = self.window_seconds - (now - queue[0])
        return max(1, int(wait + 0.999))


class RateLimiter:
    def __init__(
        self,
        per_minute: int,
        jobs_per_minute: int,
        previews_per_minute: int | None = None,
        status_per_minute: int | None = None,
    ) -> None:
        self.general = SlidingWindowCounter(per_minute, 60.0)
        self.jobs = SlidingWindowCounter(jobs_per_minute, 60.0)
        self.previews = SlidingWindowCounter(
            previews_per_minute if previews_per_minute is not None else jobs_per_minute, 60.0
        )
        self.status = SlidingWindowCounter(
            status_per_minute if status_per_minute is not None else 300, 60.0
        )

    def check(self, client_ip: str, bucket: str = "general") -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds) for the named bucket."""
        counters = {
            "general": self.general,
            "job": self.jobs,
            "preview": self.previews,
            "status": self.status,
        }
        counter = counters.get(bucket, self.general)
        if counter.allow(client_ip):
            return True, 0
        return False, counter.retry_after(client_ip)


def client_ip(headers, scope_client: tuple | None) -> str:
    real_ip = headers.get("x-real-ip")
    if real_ip:
        return real_ip
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if scope_client:
        return scope_client[0]
    return "unknown"
