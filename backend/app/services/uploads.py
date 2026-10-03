"""Uploaded source files — the third way to feed the clipper.

A user can upload a video they already have on their device instead of
pasting a YouTube URL. Uploads are deliberately TRANSIENT and metadata is
kept in a sidecar JSON next to the binary (not in the database):

- the file exists only to feed clip jobs; there is nothing to poll or
  resume, so a DB row would outlive its file on every restart and add a
  migration for no query benefit;
- the sidecar lives exactly as long as the bytes do — one glob sweep in
  the retention worker cleans both, atomically by convention;
- job creation verifies existence by reading the sidecar + statting the
  binary, so stale pointers fail with a clean 422 instead of a 500.

Layout: ``{uploads_dir}/{upload_id}.bin`` (raw bytes as uploaded) and
``{uploads_dir}/{upload_id}.json`` (``UploadMeta`` fields).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.downloader.base import VideoSource

log = logging.getLogger("clipper.uploads")

# Upload ids are uuid4().hex — 32 lowercase hex chars. Validating the shape
# before touching the filesystem keeps path traversal out of the question.
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_MAX_FILENAME_CHARS = 200


def new_upload_id() -> str:
    import uuid

    return uuid.uuid4().hex


def valid_upload_id(upload_id: str) -> bool:
    return bool(_UPLOAD_ID_RE.match(upload_id or ""))


def sanitize_filename(name: str | None) -> str:
    """A safe, human-readable display name for the uploaded file."""
    name = (name or "").replace("\\", "/").split("/")[-1]  # basename only
    name = "".join(c if (c.isprintable() and c not in '<>:"|?*') else "_" for c in name)
    name = name.strip(" .") or "upload"
    return name[:_MAX_FILENAME_CHARS]


def bin_path(settings: Settings, upload_id: str) -> Path:
    return settings.uploads_dir / f"{upload_id}.bin"


def meta_path(settings: Settings, upload_id: str) -> Path:
    return settings.uploads_dir / f"{upload_id}.json"


def save_meta(
    settings: Settings,
    *,
    upload_id: str,
    filename: str,
    size: int,
    mime: str | None,
    duration: float | None,
    width: int | None,
    height: int | None,
) -> dict:
    meta = {
        "id": upload_id,
        "filename": filename,
        "size": size,
        "mime": mime,
        "duration": duration,
        "width": width,
        "height": height,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path(settings, upload_id).write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    return meta


def load_upload(settings: Settings, upload_id: str) -> dict | None:
    """The upload's metadata dict, or None when unknown/expired/swept."""
    if not valid_upload_id(upload_id):
        return None
    try:
        meta = json.loads(meta_path(settings, upload_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("id") != upload_id:
        return None
    if not bin_path(settings, upload_id).is_file():
        return None
    return meta


def upload_as_source(settings: Settings, upload_id: str) -> VideoSource | None:
    """Wrap an uploaded file as the VideoSource the clip pipeline consumes."""
    meta = load_upload(settings, upload_id)
    if meta is None:
        return None
    return VideoSource(
        path=bin_path(settings, upload_id),
        segment_start=0.0,
        title=meta.get("filename") or "upload",
        duration=meta.get("duration"),
        provider="upload",
        metadata={"upload_id": upload_id},
    )


def sweep_uploads(settings: Settings, *, now: datetime) -> int:
    """Delete upload pairs (bin + sidecar) older than the retention window.

    Filesystem-based: the sidecar's mtime is the upload timestamp. Returns
    the number of removed pairs for the sweeper's log line.
    """
    cutoff_ts = now.timestamp() - settings.upload_retention_hours * 3600.0
    removed = 0
    uploads = settings.uploads_dir
    if not uploads.is_dir():
        return 0
    for sidecar in uploads.glob("*.json"):
        try:
            if sidecar.stat().st_mtime >= cutoff_ts:
                continue
        except OSError:  # pragma: no cover - vanished mid-glob
            continue
        try:
            sidecar.unlink(missing_ok=True)
            bin_path(settings, sidecar.stem).unlink(missing_ok=True)
            removed += 1
        except OSError:  # pragma: no cover
            log.warning("could not sweep upload %s", sidecar.stem)
    if removed:
        log.info("retention sweep removed %d upload(s)", removed)
    return removed
