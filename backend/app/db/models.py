"""Job model — the single persistent record type (spec §7).

Deliberately denormalized into one table: personal-scale tool, simple to
migrate, every field the history endpoint needs is in one row.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, JSON, Integer, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    """Microsecond-precision UTC now.

    Server-side CURRENT_TIMESTAMP is second-resolution on SQLite, which makes
    ordering/pagination non-deterministic for jobs created in the same second.
    """
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    CLIPPING = "clipping"
    COMPLETED = "completed"
    FAILED = "failed"


class PreviewStatus(str, enum.Enum):
    PENDING = "pending"
    RESOLVING = "resolving"  # asking a provider for an instant-playback stream
    STREAMING = "streaming"  # playable via the proxy; background download running
    DOWNLOADING = "downloading"  # no direct stream — timeline only until the file lands
    PROCESSING = "processing"  # file cached + local playback; thumbnails generating
    READY = "ready"
    FAILED = "failed"
    EXPIRED = "expired"


ACTIVE_STATUSES = (JobStatus.DOWNLOADING, JobStatus.CLIPPING)
PREVIEW_ACTIVE_STATUSES = (
    PreviewStatus.PENDING,
    PreviewStatus.RESOLVING,
    PreviewStatus.STREAMING,
    PreviewStatus.DOWNLOADING,
    PreviewStatus.PROCESSING,
)
# statuses under which a queued clip job must wait for the preview's file
PREVIEW_WAITING_STATUSES = (
    PreviewStatus.PENDING,
    PreviewStatus.RESOLVING,
    PreviewStatus.STREAMING,
    PreviewStatus.DOWNLOADING,
    PreviewStatus.PROCESSING,
)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)

    # request
    source_url: Mapped[str] = mapped_column(String(2048))
    video_id: Mapped[str] = mapped_column(String(16), index=True)
    video_title: Mapped[str | None] = mapped_column(String(512))
    start_seconds: Mapped[float] = mapped_column(Float)  # stored as seconds
    end_seconds: Mapped[float] = mapped_column(Float)  # stored as seconds
    style_id: Mapped[str] = mapped_column(String(64), default="original")
    style_params: Mapped[dict] = mapped_column(JSON, default=dict)
    # optional: reuse the file a preview already downloaded for this URL
    preview_id: Mapped[str | None] = mapped_column(String(32), index=True)

    # lifecycle
    status: Mapped[JobStatus] = mapped_column(
        SAEnum(JobStatus, native_enum=False, length=16, values_callable=lambda e: [i.value for i in e]),
        default=JobStatus.QUEUED,
        index=True,
    )
    error: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)

    # result
    output_filename: Mapped[str | None] = mapped_column(String(128))
    output_size_bytes: Mapped[int | None] = mapped_column(Integer)
    output_duration_seconds: Mapped[float | None] = mapped_column(Float)

    # timestamps (UTC, microsecond precision from the app side)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )
    file_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Job {self.id[:8]} {self.status.value} {self.start_seconds}-{self.end_seconds}s>"


class Preview(Base):
    """A source-video preview: downloaded file + timeline metadata + filmstrip.

    Powers the timeline UI. The downloaded file is REUSED by clip jobs that
    pass this preview's id, so a clip-it-right-away flow downloads once.
    Files live on the ephemeral disk and are swept after the retention window;
    the row is kept (status=expired) so polling clients get a clean 410.
    """

    __tablename__ = "previews"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)

    source_url: Mapped[str] = mapped_column(String(2048))
    video_id: Mapped[str] = mapped_column(String(16), index=True)

    status: Mapped[PreviewStatus] = mapped_column(
        SAEnum(
            PreviewStatus,
            native_enum=False,
            length=16,
            values_callable=lambda e: [i.value for i in e],
        ),
        default=PreviewStatus.PENDING,
        index=True,
    )
    error: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(String(512))

    # instant-playback target resolved by a provider (played through the
    # backend's /stream proxy until the local file takes over)
    stream_url: Mapped[str | None] = mapped_column(String(2048))
    stream_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    stream_provider: Mapped[str | None] = mapped_column(String(32))

    # background-download progress, 0..1 (None = indeterminate)
    progress: Mapped[float | None] = mapped_column(Float)

    # ephemeral artifacts (absolute paths on the instance disk)
    file_path: Mapped[str | None] = mapped_column(String(1024))
    thumb_dir: Mapped[str | None] = mapped_column(String(1024))

    # timeline metadata
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    thumb_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Preview {self.id[:8]} {self.status.value} {self.source_url[:40]}"
