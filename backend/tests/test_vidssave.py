"""vidssave provider tests — all offline.

Uses the REAL protocol shape captured from vidssave.com (2026-10-03):
AES-256-CBC / ZeroPadding / base64 `data` fields, the media/parse →
media/download → SSE download_query chain, and the 302 → CDN link.
HTTP calls are mocked at the httpx boundary, mirroring the cobalt tests.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.config import Settings
from app.downloader.base import ProviderError
from app.downloader.vidssave import (
    AES_KEYS,
    VidsSaveProvider,
    _decrypt,
    _quality_number,
    unwrap,
)


def base_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        environment="test",
        log_level="WARNING",
        data_dir=str(tmp_path / "data"),
        database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}",
    )
    values.update(overrides)
    s = Settings(**values)
    s.ensure_dirs()
    return s


# ---------------- crypto ----------------


def _encrypt(payload: dict | str) -> str:
    """Encrypt exactly like vidssave does: CBC, IV=key[:16], ZeroPadding."""
    from Crypto.Cipher import AES

    key = AES_KEYS[0].encode()
    raw = json.dumps(payload) if isinstance(payload, dict) else payload
    data = raw.encode()
    pad = (-len(data)) % 16  # zero padding to a block boundary
    cipher = AES.new(key, AES.MODE_CBC, key[:16]).encrypt(data + b"\x00" * pad)
    return base64.b64encode(cipher).decode()


def test_decrypt_roundtrip_with_captured_key():
    ciphertext = _encrypt({"hello": "world", "n": 12.5})
    assert _decrypt(ciphertext) is not None
    assert json.loads(_decrypt(ciphertext)) == {"hello": "world", "n": 12.5}


def test_decrypt_rejects_garbage():
    assert _decrypt("not-base64!!!") is None
    assert _decrypt("") is None
    assert _decrypt("aGVsbG8=") is None  # valid b64, not a multiple of 16 bytes


def test_unwrap_plain_json_fast_path():
    payload = {"status": 1, "data": {"task_id": "abc"}}  # data already a dict
    assert unwrap(payload) == payload


def test_unwrap_decrypts_string_data():
    inner = {"task_id": "Z0veS55TVbA8u-demOkyhv"}
    envelope = {"status": 1, "data": _encrypt(inner), "status_code": "success"}
    result = unwrap(envelope)
    assert result["status"] == 1
    assert result["data"] == inner


def test_unwrap_string_payload_decrypts():
    inner = {"duration": 19, "title": "Me at the zoo"}
    assert unwrap(_encrypt(inner)) == inner


def test_quality_number():
    assert _quality_number("144P") == 144
    assert _quality_number("720p") == 720
    assert _quality_number("1080P") == 1080
    assert _quality_number("256KBPS") == 0  # audio bitrate — not a video height
    assert _quality_number("") == 0


# ---------------- provider flow (httpx mocked) ----------------


class _FakeStream:
    """Enough of httpx.StreamingResponse for _poll_task + _download_to_file."""

    def __init__(self, status_code: int, lines: list[str] | None = None, body: bytes = b""):
        self.status_code = status_code
        self.headers = httpx.Headers(
            {"content-type": "video/mp4", "content-length": str(len(body))}
            if body
            else {"content-type": "text/event-stream"}
        )
        self._lines = lines or []
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        return iter(self._lines)

    def iter_bytes(self, chunk_size=None):
        view = memoryview(self._body)
        for i in range(0, len(self._body), chunk_size or 65536):
            yield bytes(view[i : i + (chunk_size or 65536)])

    def close(self):
        pass


class _FakeClient:
    def __init__(self, stream: _FakeStream):
        self._stream = stream

    def build_request(self, *a, **kw):
        return httpx.Request("GET", a[1] if len(a) > 1 else kw.get("url", ""))

    def send(self, *a, **kw):
        assert kw.get("stream") is True
        return self._stream

    def close(self):
        pass


def _parse_envelope(title="Me at the zoo", duration=19, quality="240P"):
    resources = [
        {
            "resource_content": "TOKEN-144P",
            "quality": "144P",
            "format": "MP4",
            "type": "video",
            "size": 195278,
        },
        {
            "resource_content": "TOKEN-240P",
            "quality": quality,
            "format": "MP4",
            "type": "video",
            "size": 433081,
        },
        {
            "resource_content": "TOKEN-AUDIO",
            "quality": "128KBPS",
            "format": "MP3",
            "type": "audio",
            "size": 309197,
        },
    ]
    return {
        "status": 1,
        "data": _encrypt(
            {"title": title, "duration": duration, "resources": resources}
        ),
        "status_code": "success",
    }


def test_provider_full_flow(tmp_path, monkeypatch):
    settings = base_settings(tmp_path, vidssave_api_url="https://api.vidssave.com/api/contentsite_api")
    provider = VidsSaveProvider(settings)

    calls = {"n": 0}

    def fake_post(url, data=None, headers=None, timeout=None):
        calls["n"] += 1
        if url.endswith("/media/parse"):
            assert data["origin"] == "source"
            assert data["link"].startswith("https://www.youtube.com/")
            return httpx.Response(200, json=_parse_envelope(), request=httpx.Request("POST", url))
        if url.endswith("/media/download"):
            assert data["request"] == "TOKEN-240P"  # best ≤ 720p
            assert data["no_encrypt"] == "1"
            return httpx.Response(
                200,
                json={"status": 1, "data": _encrypt({"task_id": "TASK-1"})},
                request=httpx.Request("POST", url),
            )
        raise AssertionError(f"unexpected POST {url}")

    sse_lines = [
        "event: running",
        'data: {"status":"running","progress":40}',
        "",
        "event: running",
        'data: {"status":"running","progress":80}',
        "",
        "event: success",
        'data: {"status":"success","progress":100,"filesize":742417,"download_link":"https://api.vidssave.com/api/contentsite_api/media/download_redirect?request=xyz","download_type":""}',
        "",
    ]
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 4096  # plausible mp4 head

    progresses = []

    def fake_stream(method, url, **kw):
        if "download_query" in url:
            params = kw.get("params") or {}
            assert params.get("task_id") == "TASK-1"
            assert params.get("download_domain") == "vidssave.com"
            return _FakeStream(200, lines=sse_lines)
        assert "download_redirect" in url  # the final CDN hop (after 302)
        return _FakeStream(200, body=mp4)

    monkeypatch.setattr("app.downloader.vidssave.httpx.post", fake_post)
    monkeypatch.setattr("app.downloader.vidssave.httpx.stream", fake_stream)
    monkeypatch.setattr(
        "app.downloader.vidssave.ffprobe_duration", lambda path: 18.93
    )

    source = provider.get_video(
        "https://www.youtube.com/watch?v=jNQXAC9IVRw", 0, 10,
        progress=lambda r: progresses.append(r),
    )
    assert source.provider == "vidssave"
    assert source.title == "Me at the zoo"
    assert source.duration == 18.93
    assert source.path.name.startswith("vidssave_")
    assert source.path.read_bytes() == mp4
    assert source.metadata["quality"] == "240P"
    # progress was reported from the SSE running frames (0.40, 0.80)
    assert any(abs(p - 0.40) < 0.01 for p in progresses)
    source.path.unlink()


def test_provider_falls_back_to_dev_api(tmp_path, monkeypatch):
    """Prod parse refuses (analyze_risk) → the dev endpoint serves the data."""
    settings = base_settings(tmp_path)  # no override -> prod + dev chain
    provider = VidsSaveProvider(settings)

    def fake_post(url, data=None, headers=None, timeout=None):
        if url.startswith("https://api.vidssave.com"):
            return httpx.Response(
                200, json={"msg": "analyze failed", "status": 0, "status_code": "analyze_risk"},
                request=httpx.Request("POST", url),
            )
        assert url.startswith("https://test-api.vidssave.com")
        return httpx.Response(200, json=_parse_envelope(), request=httpx.Request("POST", url))

    monkeypatch.setattr("app.downloader.vidssave.httpx.post", fake_post)

    with pytest.raises(ProviderError) as exc:
        # parse succeeds via dev, but the download task also hits prod-first
        # and fails there too — with no working task host the error surfaces.
        provider.get_video("https://www.youtube.com/watch?v=jNQXAC9IVRw", 0, 5)
    assert exc.value.provider == "vidssave"


def test_provider_analyze_risk_on_both_hosts(tmp_path, monkeypatch):
    settings = base_settings(tmp_path)
    provider = VidsSaveProvider(settings)

    def refused(url, **kw):
        return httpx.Response(
            200,
            json={"msg": "analyze failed", "status": 0, "status_code": "analyze_risk"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("app.downloader.vidssave.httpx.post", refused)
    with pytest.raises(ProviderError) as exc:
        provider.get_video("https://www.youtube.com/watch?v=jNQXAC9IVRw", 0, 5)
    assert "analyze_risk" in exc.value.message


def test_provider_disabled(tmp_path):
    settings = base_settings(tmp_path, vidssave_enabled=False)
    provider = VidsSaveProvider(settings)
    with pytest.raises(ProviderError) as exc:
        provider.get_video("https://www.youtube.com/watch?v=jNQXAC9IVRw", 0, 5)
    assert "disabled" in exc.value.message


def test_registry_knows_vidssave():
    from app.downloader.registry import PROVIDERS

    assert "vidssave" in PROVIDERS
    assert PROVIDERS["vidssave"] is VidsSaveProvider


def test_default_chain_starts_with_vidssave(monkeypatch):
    monkeypatch.delenv("CLIPPER_DOWNLOADER_PROVIDERS", raising=False)
    # _env_file=None ignores the developer's local backend/.env overrides
    settings = Settings(environment="test", _env_file=None)
    assert settings.provider_chain[0] == "vidssave"
