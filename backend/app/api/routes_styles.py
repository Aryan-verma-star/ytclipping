"""Style listing endpoint — the UI populates itself dynamically from this."""

from __future__ import annotations

from fastapi import APIRouter

from app.styles.base import all_styles, public_style

router = APIRouter(tags=["styles"])


@router.get("/styles")
def list_styles() -> list[dict]:
    return [public_style(s) for s in all_styles()]
