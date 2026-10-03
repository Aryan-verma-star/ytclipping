"""Application configuration.

Every tunable lives here and is overridable via environment variables
(see .env.example at the repository root). No secrets are hardcoded.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]  # .../backend
REPO_ROOT = BACKEND_DIR.parent  # repository root


class Settings(BaseSettings):
    """All environment variables are namespaced with CLIPPER_ to avoid
    collisions in shared environments (e.g. a platform-injected DATABASE_URL
    for a different service — exactly what happens on Render and in this
    build sandbox)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="CLIPPER_",
        case_sensitive=False,
        extra="ignore",
    )

    # --- application -------------------------------------------------
    app_name: str = "YouTube Clipper"
    environment: str = "development"  # development | production
    log_level: str = "INFO"
    # Comma-separated list of EXTRA origins allowed to call the API from a
    # browser (CORS). Only needed for split deployments where the frontend
    # is hosted on a different domain than the backend (e.g. frontend on
    # Vercel, backend on Render). Empty = same-origin only (no CORS headers).
    allowed_origins: str = ""

    # --- persistence ---------------------------------------------------
    # Empty DATABASE_URL falls back to a local SQLite file under DATA_DIR.
    # Production (Render + Neon): postgresql://user:pass@host/dbname
    database_url: str = ""
    data_dir: str = ""  # default: backend/data
    frontend_dir: str = ""  # default: repo_root/frontend
    init_db_on_startup: bool = True

    # --- validation limits ---------------------------------------------
    max_clip_seconds: float = 600.0  # user decision: 10 minutes
    max_source_seconds: float = 14400.0  # user decision: 4 hours

    # --- downloader providers ------------------------------------------
    # Ordered chain, comma separated. Known providers: cobalt, ytdlp, sample.
    downloader_providers: str = "cobalt,ytdlp"
    cobalt_api_url: str = ""  # e.g. your cobalt instance URL (REQUIRED for cobalt)
    cobalt_api_key: str = ""  # optional bearer token for instances that require auth
    cobalt_timeout_seconds: float = 30.0
    ytdlp_timeout_seconds: float = 900.0
    ytdlp_cookies_file: str = ""  # optional cookies.txt path for datacenter-IP bot checks
    # ALTERNATIVE to the file: raw cookies.txt CONTENT pasted as an env var
    # (e.g. Render dashboard → Environment). Written to a temp file at startup
    # when ytdlp_cookies_file is empty — the friendliest path for PaaS hosting.
    ytdlp_cookies: str = ""
    ytdlp_extra_args: str = ""  # extra CLI args, space separated
    max_video_height: int = 720  # lowest-sufficient-quality policy
    max_download_bytes: int = 1_500_000_000  # hard cap on a single source download
    sample_max_seconds: float = 120.0  # dev/test provider: max synthesized duration

    # --- retention -------------------------------------------------------
    clip_retention_hours: float = 24.0  # metadata is kept forever, files are not
    retention_sweep_interval_seconds: float = 900.0

    # --- preview pipeline (timeline UI) -----------------------------------
    preview_thumb_count: int = 60  # filmstrip tiles generated per preview
    preview_thumb_height: int = 120  # tile height in px (width follows aspect)
    preview_retention_hours: float = 24.0  # preview files swept after this
    preview_max_processing_minutes: float = 30.0  # hard wall for a stuck preview

    # --- job worker -------------------------------------------------------
    worker_enabled: bool = True
    worker_poll_interval_seconds: float = 1.0

    # --- abuse protection --------------------------------------------------
    rate_limit_per_minute: int = 60  # general API requests per client IP
    rate_limit_jobs_per_minute: int = 6  # job creations per client IP
    rate_limit_previews_per_minute: int = 6  # preview (full download!) per IP
    max_request_bytes: int = 65_536  # request body cap (we only ever submit URLs)

    # --- phase 4 (AI) ------------------------------------------------------
    ai_analyzer_enabled: bool = False

    # --- computed paths ------------------------------------------------------
    @property
    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir).resolve() if self.data_dir else BACKEND_DIR / "data"

    @property
    def clips_dir(self) -> Path:
        return self.resolved_data_dir / "clips"

    @property
    def previews_dir(self) -> Path:
        return self.resolved_data_dir / "previews"

    @property
    def tmp_dir(self) -> Path:
        return self.resolved_data_dir / "tmp"

    @property
    def resolved_frontend_dir(self) -> Path:
        return Path(self.frontend_dir).resolve() if self.frontend_dir else REPO_ROOT / "frontend"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def provider_chain(self) -> list[str]:
        return [p.strip() for p in self.downloader_providers.split(",") if p.strip()]

    @property
    def resolved_database_url(self) -> str:
        url = (self.database_url or "").strip()
        if not url:
            return f"sqlite:///{(self.resolved_data_dir / 'clips.db').as_posix()}"
        # Accept the legacy Heroku-style scheme; reject anything unparseable
        # early with a clear message instead of a cryptic SQLAlchemy error.
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if not (url.startswith("sqlite://") or url.startswith("postgresql://")):
            raise ValueError(
                f"CLIPPER_DATABASE_URL must start with sqlite:// or postgresql:// "
                f"(got {url[:60]!r}). Note: this app does NOT read the generic "
                "DATABASE_URL variable — use the CLIPPER_ prefix."
            )
        return url

    def ensure_dirs(self) -> None:
        self.resolved_data_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.previews_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
