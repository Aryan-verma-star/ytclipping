"""CORS behaviour (split deployments: frontend on another origin).

CLIPPER_ALLOWED_ORIGINS turns on permissive CORS for exactly the listed
origins — needed only when the static frontend is hosted elsewhere (e.g.
Vercel) while the API runs on Render. Default (unset) keeps the API
same-origin only: no CORS headers at all.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import make_settings

ALLOWED = "https://youtube-clipper.vercel.app"


def make_client(settings: Settings) -> TestClient:
    with TestClient(create_app(settings)) as client:
        yield client


def test_cors_property_parsing(tmp_path):
    s = make_settings(tmp_path, allowed_origins=f"  {ALLOWED} , https://a.test ,, ")
    assert s.cors_origins == [ALLOWED, "https://a.test"]


def test_default_has_no_cors_headers(tmp_path):
    """Same-origin deployment: no CORS middleware, no CORS headers."""
    with TestClient(create_app(make_settings(tmp_path))) as client:
        r = client.get("/api/meta", headers={"Origin": ALLOWED})
        assert r.status_code == 200
        assert "access-control-allow-origin" not in r.headers


def test_preflight_from_allowed_origin(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, allowed_origins=ALLOWED))) as client:
        r = client.options(
            "/api/jobs",
            headers={
                "Origin": ALLOWED,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == ALLOWED
        assert "POST" in r.headers["access-control-allow-methods"]
        assert "content-type" in r.headers["access-control-allow-headers"].lower()


def test_simple_get_from_allowed_origin(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, allowed_origins=ALLOWED))) as client:
        r = client.get("/api/meta", headers={"Origin": ALLOWED})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == ALLOWED


def test_preflight_from_disallowed_origin_rejected(tmp_path):
    with TestClient(create_app(make_settings(tmp_path, allowed_origins=ALLOWED))) as client:
        r = client.options(
            "/api/jobs",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.status_code == 400  # CORSMiddleware refuses disallowed origins
        assert r.headers.get("access-control-allow-origin") != "https://evil.example"


def test_range_header_allowed_for_video_streaming(tmp_path):
    """Cross-origin <video> seeking needs Range on the allow list."""
    with TestClient(create_app(make_settings(tmp_path, allowed_origins=ALLOWED))) as client:
        r = client.options(
            "/api/jobs/x/clip",
            headers={
                "Origin": ALLOWED,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "range",
            },
        )
        assert r.status_code == 200
        assert "range" in r.headers["access-control-allow-headers"].lower()


def test_health_still_works_with_cors_enabled(tmp_path):
    """CORS middleware must not break the Render health check (no Origin)."""
    with TestClient(create_app(make_settings(tmp_path, allowed_origins=ALLOWED))) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
