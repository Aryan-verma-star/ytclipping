"""Upload endpoints — feed the clipper with a local video file.

``POST /api/uploads`` (multipart/form-data, field ``file``) streams the body
to disk in 1 MB chunks (RAM stays flat), enforces CLIPPER_MAX_UPLOAD_BYTES
itself, and probes the result with ffprobe. A file without a decodable
video stream is rejected with a clean 422 — and the bytes are deleted.

The response metadata is what ``POST /api/jobs`` needs later via
``upload_id``; the frontend keeps playing/previewing the user's LOCAL copy
in the meantime (no round trip just to show a timeline).

``GET /api/uploads/{id}`` returns the same metadata — handy for debugging
and for clients that lost the POST response.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Request, UploadFile
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.core.errors import PayloadTooLargeError, ValidationAppError
from app.core.ffmpeg import ffprobe_video_info
from app.services import uploads as upload_store

router = APIRouter(tags=["uploads"])

_CHUNK_BYTES = 1024 * 1024  # stream to disk in 1 MB slices


class UploadOut(BaseModel):
    id: str
    filename: str
    size: int
    mime: str | None
    duration: float | None
    width: int | None
    height: int | None
    created_at: str


def _to_out(meta: dict) -> UploadOut:
    return UploadOut(
        id=meta["id"],
        filename=meta.get("filename") or "upload",
        size=int(meta.get("size") or 0),
        mime=meta.get("mime"),
        duration=meta.get("duration"),
        width=meta.get("width"),
        height=meta.get("height"),
        created_at=meta.get("created_at") or "",
    )


@router.post("/uploads", response_model=UploadOut)
async def create_upload(request: Request, file: UploadFile = File(...)) -> UploadOut:
    settings = request.app.state.settings
    upload_id = upload_store.new_upload_id()
    target = upload_store.bin_path(settings, upload_id)

    size = 0
    try:
        with open(target, "wb") as out:
            while True:
                chunk = await file.read(_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise PayloadTooLargeError(
                        f"The uploaded file is larger than the "
                        f"{settings.max_upload_bytes // 1_000_000} MB limit "
                        f"({settings.max_upload_bytes} bytes)."
                    )
                out.write(chunk)
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        raise
    finally:
        await file.close()

    if size == 0:
        target.unlink(missing_ok=True)
        raise ValidationAppError("The uploaded file is empty.", field="file")

    try:
        duration, width, height = await run_in_threadpool(ffprobe_video_info, target)
    except Exception:  # pragma: no cover - probe errors are (None, None, None)
        duration = width = height = None

    if duration is None:
        target.unlink(missing_ok=True)
        raise ValidationAppError(
            "The uploaded file does not look like a video ffprobe can read "
            "(no decodable stream found). Try a standard mp4/webm/mov file.",
            field="file",
        )

    meta = upload_store.save_meta(
        settings,
        upload_id=upload_id,
        filename=upload_store.sanitize_filename(file.filename),
        size=size,
        mime=file.content_type,
        duration=duration,
        width=width,
        height=height,
    )
    return _to_out(meta)


@router.get("/uploads/{upload_id}", response_model=UploadOut)
def get_upload(upload_id: str, request: Request) -> UploadOut:
    settings = request.app.state.settings
    meta = upload_store.load_upload(settings, upload_id)
    if meta is None:
        from app.core.errors import NotFoundError

        raise NotFoundError(
            "Upload not found — it may have expired (files are kept "
            f"{settings.upload_retention_hours:g} h) or the service restarted. "
            "Upload the file again."
        )
    return _to_out(meta)
