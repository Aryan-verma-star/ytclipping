"""AI suggestion endpoint — Phase 4 STUB (spec §3 Phase 4).

Deliberately returns 501 with an explanation of the extension point.
When the future analyzer repository is integrated, this endpoint will call
app.ai.analyzer.get_analyzer().analyze(url) and return suggested segments.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["ai"])


class SuggestRequest(BaseModel):
    url: str
    max_suggestions: int | None = None


@router.post("/ai/suggest", status_code=501)
def suggest(payload: SuggestRequest) -> dict:
    return {
        "error": {
            "code": "not_implemented",
            "message": (
                "AI clip-segment analysis is Phase 4 and intentionally not "
                "implemented in this build. The extension point is defined in "
                "backend/app/ai/analyzer.py (VideoAnalyzer protocol) — see "
                "docs/ai-integration.md for how a future analyzer module plugs "
                "in and feeds suggestions into POST /api/jobs."
            ),
        }
    }
