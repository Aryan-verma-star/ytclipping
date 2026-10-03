"""Offline tests for the media proxy (browser-engine byte relay).

Covers the retry loop added 2026-10-03: vidssave's CDN serves lazily
materialized files that 403 for the first ~30-60 s, so the proxy retries
transient upstream refusals with a backoff instead of surfacing a 502 the
<video> element cannot recover from. httpx.Client is faked; no network.
"""

from __future__ import annotations

import io

import httpx
import pytest

from app.api import routes_media


class FakeResponse:
    def __init__(self, status_code: int, body: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.headers = httpx.Headers(headers or {})
        self.closed = False

    def close(self):
        self.closed = True

    def iter_bytes(self, chunk_size: int = 65536):
        yield self._body


class FakeClient:
    """Returns the queued responses (cycled); records every request."""

    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.closed = False

    def build_request(self, method: str, url: str, headers: dict | None = None):
        return httpx.Request(method, url, headers=headers)

    def send(self, request, stream: bool = False):
        self.requests.append(request)
        resp = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return resp

    def close(self):
        self.closed = True


@pytest.fixture()
def fast_backoff(monkeypatch):
    """No real sleeping in tests."""
    monkeypatch.setattr(routes_media.time, "sleep", lambda s: None)


CDN_URL = "https://down-de.vidssave.com/tmp/recycle/1m/content_site/download/aa/77/file-123.mp4"


def _install(monkeypatch, client: FakeClient):
    monkeypatch.setattr(routes_media.httpx, "Client", lambda **kw: client)


def test_proxy_retries_transient_403_then_streams(client, monkeypatch, fast_backoff):
    fake = FakeClient([
        FakeResponse(403, b"Forbidden"),
        FakeResponse(403, b"Forbidden"),
        FakeResponse(200, b"MP4DATA", {"content-type": "video/mp4",
                                       "content-length": "7"}),
    ])
    _install(monkeypatch, fake)

    resp = client.get("/api/media/proxy", params={"url": CDN_URL})
    assert resp.status_code == 200
    assert resp.content == b"MP4DATA"
    assert resp.headers["content-type"] == "video/mp4"
    # exactly three upstream attempts: 403, 403, 200
    assert len(fake.requests) == 3
    # full vidssave identity on every attempt: browser User-Agent (the CDN
    # 403s non-browser agents) AND Origin/Referer vidssave.com (required
    # from datacenter IPs like Render's — verified live 2026-10-03)
    for req in fake.requests:
        assert "Mozilla/5.0" in req.headers["user-agent"]
        assert "Clipper" not in req.headers["user-agent"]
        assert req.headers.get("origin") == "https://vidssave.com"
        assert req.headers.get("referer") == "https://vidssave.com/"


def test_proxy_passes_range_header_upstream(client, monkeypatch, fast_backoff):
    fake = FakeClient([
        FakeResponse(206, b"12345", {"content-type": "video/mp4",
                                     "content-range": "bytes 0-4/10"}),
    ])
    _install(monkeypatch, fake)

    resp = client.get("/api/media/proxy", params={"url": CDN_URL},
                      headers={"Range": "bytes=0-4"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 0-4/10"
    assert fake.requests[0].headers.get("range") == "bytes=0-4"


def test_proxy_fails_fast_on_non_retryable(client, monkeypatch, fast_backoff):
    """404 is permanent — one attempt, no retry, 502 to the browser."""
    fake = FakeClient([FakeResponse(404, b"nope")])
    _install(monkeypatch, fake)

    resp = client.get("/api/media/proxy", params={"url": CDN_URL})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_error"
    assert "404" in resp.json()["error"]["message"]
    assert len(fake.requests) == 1


def test_proxy_exhausts_retries_with_502(client, monkeypatch, fast_backoff):
    fake = FakeClient([FakeResponse(403, b"Forbidden")])  # always 403
    _install(monkeypatch, fake)

    resp = client.get("/api/media/proxy", params={"url": CDN_URL})
    assert resp.status_code == 502
    assert "after several retries" in resp.json()["error"]["message"]
    # one attempt per configured delay
    assert len(fake.requests) == len(routes_media._RETRY_DELAYS)


def test_proxy_rejects_non_vidssave_hosts(client):
    for url in (
        "https://evil.example.com/file.mp4",
        "https://vidssave.com.evil.example.com/file.mp4",
        "http://api.vidssave.com/file.mp4",  # http downgrade
        "https://vidssave.com/x",
    ):
        resp = client.get("/api/media/proxy", params={"url": url})
        assert resp.status_code == 403, url
        assert resp.json()["error"]["code"] == "forbidden_url"


def test_proxy_requires_url_param(client):
    resp = client.get("/api/media/proxy")
    assert resp.status_code == 422
