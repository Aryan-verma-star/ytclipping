"""Shared fixtures.

The test suite never touches third-party sites (spec §3 Phase 1): happy-path
jobs run through the local `sample` provider (ffmpeg-synthesized video), and
failure paths use in-test fake providers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.downloader.base import DownloaderProvider, ProviderError, VideoSource
from app.main import create_app
from app.services.orchestrator import process_job


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        environment="test",
        log_level="WARNING",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        data_dir=str(tmp_path / "data"),
        frontend_dir=str(tmp_path),  # exists, empty — static mount is a no-op
        downloader_providers="sample",
        worker_enabled=False,  # tests drive process_job() deterministically
        init_db_on_startup=True,  # exercises the real alembic migration path
        rate_limit_per_minute=100000,
        rate_limit_jobs_per_minute=100000,
        rate_limit_previews_per_minute=100000,
        rate_limit_status_per_minute=100000,  # tight poll loops in tests
        max_video_height=240,  # fast synthetic encodes
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture()
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture()
def client(app) -> TestClient:
    with TestClient(app) as c:
        yield c


class AlwaysFailProvider(DownloaderProvider):
    """Deterministic failing provider for failure-path tests."""

    name = "always_fail"

    def get_video(self, url: str, start: float, end: float) -> VideoSource:
        raise ProviderError("injected failure for tests", provider=self.name)


def post_job(
    client: TestClient,
    url: str = "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    start="0:02",
    end="0:07",
    style_id: str = "original",
    style_params: dict | None = None,
    preview_id: str | None = None,
):
    payload = {"url": url, "start_time": start, "end_time": end, "style_id": style_id}
    if style_params is not None:
        payload["style_params"] = style_params
    if preview_id is not None:
        payload["preview_id"] = preview_id
    return client.post("/api/jobs", json=payload)


def run_job(client: TestClient, job_id: str) -> None:
    """Drive one job through the pipeline synchronously (like the worker would)."""
    process_job(job_id, client.app.state.db, client.app.state.settings, client.app.state.providers)
