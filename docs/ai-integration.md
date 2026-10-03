# Phase 4 AI Integration Guide (extension point — NOT implemented)

This build intentionally ships **no AI logic** (spec §3 Phase 4). What exists
is a clean, documented seam where the future analyzer — e.g. the prebuilt
open-source GitHub repository you will supply — plugs in with zero changes
to the job pipeline, persistence, or styles.

## The contract

`backend/app/ai/analyzer.py` defines:

```python
@dataclass(frozen=True)
class SuggestedSegment:
    start: float          # seconds
    end: float            # seconds
    label: str            # short title, e.g. "Key moment: the demo"
    reason: str = ""      # why this segment was chosen
    confidence: float | None = None

@runtime_checkable
class VideoAnalyzer(Protocol):
    id: str
    name: str
    def analyze(self, url: str, **kwargs) -> list[SuggestedSegment]: ...
```

plus a registry: `register_analyzer()` / `get_analyzer()` / `available_analyzers()`.

## Where your repository plugs in

1. Create `backend/app/ai/<your_analyzer>.py` that implements the protocol by
   wrapping the repository you choose (download/vendor it, expose its
   segmentation logic as `analyze()`).
2. In that module, call `register_analyzer(MyAnalyzer())` at import time.
3. Register the module import in `backend/app/ai/__init__.py` (one line) —
   or extend the styles-style autodiscovery if you add several.
4. Flip `CLIPPER_AI_ANALYZER_ENABLED=true`.

## Wiring into the existing flow

The stub endpoint `POST /api/ai/suggest` (currently `501 Not Implemented`,
see `backend/app/api/routes_ai.py`) is where the route lands:

```
POST /api/ai/suggest {url}
  → analyzer = get_analyzer()
  → suggestions = analyzer.analyze(url)     # list[SuggestedSegment]
  → client (or server) validates each window with the same rules as
    POST /api/jobs (MAX_CLIP_SECONDS, MAX_SOURCE_SECONDS, start<end)
  → for each accepted suggestion: POST /api/jobs
    {url, start_time: s.start, end_time: s.end, style_id, style_params}
```

Because suggestions flow through the ordinary job-creation endpoint, every
downstream guarantee applies for free: validation, rate limiting, provider
chain, trimming, persistence, and history.

## Implementation notes for the analyzer module

- `analyze()` must not crash the app: raise (or return fewer suggestions)
  with a clear message; the route converts exceptions to a 4xx/5xx JSON error.
- Validate/clamp your own outputs against the app limits before returning.
- If the analyzer needs the video bytes (not just the URL), it can call the
  existing downloader provider chain — see `app.services.orchestrator.
  _download_with_chain` for the pattern — or reuse the `sample` provider in
  tests to stay offline.
- Add unit tests with a fake analyzer (see `tests/test_api_jobs.py::
  test_ai_stub_returns_501_with_docs_pointer` for the stub contract).
