"""Downloader provider tests — all offline (spec §3 Phase 1: mock the provider).

- SampleProvider: real local synthesis, exercising the pipeline.
- YtDlpProvider: command construction only (no network).
- CobaltProvider: HTTP behavior mocked at the httpx boundary.
- Chain fallback logic: first provider fails → second serves.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.downloader.base import DownloaderProvider, ProviderError, VideoSource
from app.downloader.cobalt import CobaltProvider
from app.downloader.registry import PROVIDERS, build_provider_chain
from app.downloader.sample import SampleProvider
from app.downloader.ytdlp import YtDlpProvider
from app.services.orchestrator import _download_with_chain


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


# ---------------- sample provider ----------------

def test_sample_provider_synthesizes_real_video(tmp_path):
    settings = base_settings(tmp_path, max_video_height=240)
    provider = SampleProvider(settings)
    source = provider.get_video("https://youtu.be/aBcDeFgHiJk", 0, 4)
    assert source.provider == "sample"
    assert source.segment_start == 0.0
    assert source.path.exists() and source.path.stat().st_size > 1000
    assert source.duration is not None and source.duration >= 4.0
    assert source.title
    # cleanup contract: caller (orchestrator) removes the temp file
    source.path.unlink()


def test_sample_provider_respects_duration_cap(tmp_path):
    settings = base_settings(tmp_path, sample_max_seconds=6, max_video_height=240)
    provider = SampleProvider(settings)
    source = provider.get_video("https://youtu.be/aBcDeFgHiJk", 0, 300)
    assert source.duration <= 6.5
    source.path.unlink()


# ---------------- yt-dlp provider (command construction) ----------------

def test_ytdlp_command_uses_sections_and_quality(tmp_path):
    settings = base_settings(
        tmp_path,
        ytdlp_cookies_file="/tmp/cookies.txt",
        max_video_height=480,
        ytdlp_extra_args="--extractor-args youtube:player_client=web",
    )
    provider = YtDlpProvider(settings)
    cmd = provider.build_command(
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        10.0,
        20.0,
        Path("/tmp/dl"),
    )
    joined = " ".join(cmd)
    assert "--download-sections" in cmd
    assert "*10.000-20.000" in cmd
    assert "--force-keyframes-at-cuts" in cmd
    assert "bv*[height<=480]+ba" in joined
    assert "--cookies" in cmd and "/tmp/cookies.txt" in cmd
    assert "--extractor-args" in cmd
    assert cmd[-1].startswith("https://")
    assert "--no-playlist" in cmd


def test_ytdlp_command_without_cookies(tmp_path):
    settings = base_settings(tmp_path)
    provider = YtDlpProvider(settings)
    cmd = provider.build_command("https://youtu.be/x", 0, 5, Path("/tmp/dl"))
    assert "--cookies" not in cmd


def test_ytdlp_cookies_from_env_written_to_file(tmp_path):
    """CLIPPER_YTDLP_COOKIES (content) materializes as a 0600 file the
    provider passes to yt-dlp — the Render-friendly configuration path."""
    content = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tCONSENT\tYES"
    settings = base_settings(tmp_path, ytdlp_cookies=content)
    provider = YtDlpProvider(settings)
    # file written inside the configured data dir, with restrictive perms
    cookies_path = Path(settings.resolved_data_dir) / "cookies-from-env.txt"
    assert provider._cookies_arg == str(cookies_path)
    assert cookies_path.exists()
    assert cookies_path.read_text(encoding="utf-8").startswith(content)
    assert (cookies_path.stat().st_mode & 0o777) == 0o600
    cmd = provider.build_command("https://youtu.be/x", 0, 5, Path("/tmp/dl"))
    assert "--cookies" in cmd and str(cookies_path) in cmd


def test_ytdlp_cookies_file_path_wins_over_env_content(tmp_path):
    settings = base_settings(
        tmp_path, ytdlp_cookies_file="/tmp/cookies.txt", ytdlp_cookies="junk"
    )
    provider = YtDlpProvider(settings)
    assert provider._cookies_arg == "/tmp/cookies.txt"
    # env content must NOT be materialized when the path is set
    assert not (Path(settings.resolved_data_dir) / "cookies-from-env.txt").exists()


# ---------------- cobalt provider (mocked HTTP) ----------------

@pytest.fixture()
def cobalt_settings(tmp_path):
    return base_settings(tmp_path, cobalt_api_url="https://cobalt.example/api", cobalt_api_key="tok")


def _fake_response(status_code: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload or {},
        request=httpx.Request("POST", "https://cobalt.example/api"),
    )


def test_cobalt_tunnel_download(tmp_path, cobalt_settings, monkeypatch):
    provider = CobaltProvider(cobalt_settings)

    monkeypatch.setattr(
        "app.downloader.cobalt.httpx.post",
        lambda *a, **kw: _fake_response(200, {"status": "tunnel", "url": "https://dl.example/file.mp4", "filename": "my_video.mp4"}),
    )

    def fake_stream(url, target, progress=None):
        target.write_bytes(b"FAKEVIDEO" * 1000)
        if progress:
            progress(1.0)

    monkeypatch.setattr(provider, "_stream_to_file", fake_stream)

    source = provider.get_video("https://youtu.be/aBcDeFgHiJk", 0, 5)
    assert source.provider == "cobalt"
    assert source.segment_start == 0.0
    assert source.title == "my_video.mp4"
    assert source.path.name.endswith(".mp4")


def test_cobalt_sends_auth_and_quality(tmp_path, cobalt_settings, monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update({"url": url, "json": json, "headers": headers})
        return _fake_response(200, {"status": "error", "error": {"code": "error.api.auth.jwt.missing"}})

    monkeypatch.setattr("app.downloader.cobalt.httpx.post", fake_post)
    provider = CobaltProvider(cobalt_settings)
    with pytest.raises(ProviderError) as exc:
        provider.get_video("https://youtu.be/aBcDeFgHiJk", 0, 5)
    assert exc.value.provider == "cobalt"
    assert "error.api.auth.jwt.missing" in exc.value.message
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["json"]["videoQuality"] in ("360", "480", "720", "1080")


@pytest.mark.parametrize(
    "status_code,payload,fragment",
    [
        (401, None, "credentials"),
        (403, None, "credentials"),
        (429, None, "rate-limited"),
        (500, None, "HTTP 500"),
        (200, {"status": "error", "error": {"code": "error.api.service.unavailable"}}, "error.api.service.unavailable"),
        (200, {"status": "picker", "picker": [{"type": "video"}]}, "picker"),
        (200, {"status": "weird-new-shape"}, "unexpected"),
    ],
)
def test_cobalt_failure_modes(tmp_path, cobalt_settings, monkeypatch, status_code, payload, fragment):
    monkeypatch.setattr(
        "app.downloader.cobalt.httpx.post",
        lambda *a, **kw: _fake_response(status_code, payload),
    )
    provider = CobaltProvider(cobalt_settings)
    with pytest.raises(ProviderError) as exc:
        provider.get_video("https://youtu.be/aBcDeFgHiJk", 0, 5)
    assert fragment.lower() in exc.value.message.lower()
    assert exc.value.provider == "cobalt"


def test_cobalt_transient_flag_on_rate_limit(tmp_path, cobalt_settings, monkeypatch):
    monkeypatch.setattr(
        "app.downloader.cobalt.httpx.post",
        lambda *a, **kw: _fake_response(429),
    )
    provider = CobaltProvider(cobalt_settings)
    with pytest.raises(ProviderError) as exc:
        provider.get_video("https://youtu.be/aBcDeFgHiJk", 0, 5)
    assert exc.value.transient is True


def test_cobalt_unconfigured_is_clear_error(tmp_path):
    settings = base_settings(tmp_path)  # no COBALT_API_URL
    provider = CobaltProvider(settings)
    with pytest.raises(ProviderError) as exc:
        provider.get_video("https://youtu.be/aBcDeFgHiJk", 0, 5)
    assert "COBALT_API_URL" in exc.value.message


def test_cobalt_non_json_body(tmp_path, cobalt_settings, monkeypatch):
    def fake_post(*a, **kw):
        return httpx.Response(
            200,
            text="<html>captcha page</html>",
            request=httpx.Request("POST", "https://cobalt.example/api"),
        )

    monkeypatch.setattr("app.downloader.cobalt.httpx.post", fake_post)
    provider = CobaltProvider(cobalt_settings)
    with pytest.raises(ProviderError) as exc:
        provider.get_video("https://youtu.be/aBcDeFgHiJk", 0, 5)
    assert "non-JSON" in exc.value.message


# ---------------- instant-stream resolution ----------------


def test_cobalt_resolve_stream_returns_direct_url(tmp_path, cobalt_settings, monkeypatch):
    """The instant-load contract: one API call → direct URL + probed duration."""
    monkeypatch.setattr(
        "app.downloader.cobalt.httpx.post",
        lambda *a, **kw: _fake_response(
            200,
            {"status": "tunnel", "url": "https://dl.example/file.mp4", "filename": "my_video.mp4"},
        ),
    )
    monkeypatch.setattr(
        "app.downloader.cobalt.ffprobe_remote_info",
        lambda url, headers=None, **kw: (91.5, 1280, 720),
    )
    provider = CobaltProvider(cobalt_settings)
    target = provider.resolve_stream("https://youtu.be/aBcDeFgHiJk", 0, 600)
    assert target is not None and target.playable
    assert target.url == "https://dl.example/file.mp4"
    assert target.title == "my_video.mp4"
    assert target.duration == 91.5
    assert target.provider == "cobalt"


def test_cobalt_resolve_stream_unconfigured_returns_none(tmp_path):
    settings = base_settings(tmp_path)  # no COBALT_API_URL
    provider = CobaltProvider(settings)
    assert provider.resolve_stream("https://youtu.be/aBcDeFgHiJk", 0, 600) is None


def test_cobalt_resolve_stream_api_error_returns_none(tmp_path, cobalt_settings, monkeypatch):
    monkeypatch.setattr(
        "app.downloader.cobalt.httpx.post",
        lambda *a, **kw: _fake_response(429),
    )
    provider = CobaltProvider(cobalt_settings)
    assert provider.resolve_stream("https://youtu.be/aBcDeFgHiJk", 0, 600) is None


def test_cobalt_get_video_reuses_resolved_url(tmp_path, cobalt_settings, monkeypatch):
    """The preview flow must not ask the cobalt instance twice for one video."""
    calls = []

    def counting_post(*a, **kw):
        calls.append(1)
        return _fake_response(
            200,
            {"status": "tunnel", "url": "https://dl.example/file.mp4", "filename": "v.mp4"},
        )

    monkeypatch.setattr("app.downloader.cobalt.httpx.post", counting_post)

    def fake_stream(url, target, progress=None):
        target.write_bytes(b"FAKEVIDEO" * 10)

    provider = CobaltProvider(cobalt_settings)
    monkeypatch.setattr(provider, "_stream_to_file", fake_stream)

    from app.downloader.base import StreamTarget

    resolved = StreamTarget(
        provider="cobalt",
        url="https://dl.example/file.mp4",
        title="v.mp4",
        duration=12.0,
    )
    source = provider.get_video("https://youtu.be/aBcDeFgHiJk", 0, 10, reuse=resolved)
    assert source.provider == "cobalt"
    assert calls == []  # resolved URL reused — zero extra API calls


def test_sample_resolve_stream_gives_instant_duration(tmp_path):
    """No remote URL, but the timeline can render before synthesis starts."""
    settings = base_settings(tmp_path, sample_max_seconds=45)
    provider = SampleProvider(settings)
    target = provider.resolve_stream("https://youtu.be/aBcDeFgHiJk", 0, 14400)
    assert target is not None
    assert target.playable is False
    assert target.url is None
    assert target.duration == 45.0


def test_ytdlp_resolve_stream_selects_muxed_format(tmp_path, monkeypatch):
    settings = base_settings(tmp_path)
    provider = YtDlpProvider(settings)

    class FakeProc:
        returncode = 0
        stdout = (
            '{"title": "Some video", "duration": 62.0, "url": "https://media.example/v.mp4", '
            '"vcodec": "avc1.64001f", "acodec": "mp4a.40.2", '
            '"http_headers": {"User-Agent": "UA/1.0"}}'
        )
        stderr = ""

    monkeypatch.setattr("app.downloader.ytdlp.subprocess.run", lambda *a, **kw: FakeProc())
    target = provider.resolve_stream("https://youtu.be/aBcDeFgHiJk", 0, 600)
    assert target is not None and target.playable
    assert target.url == "https://media.example/v.mp4"
    assert target.title == "Some video"
    assert target.duration == 62.0
    assert target.headers["User-Agent"] == "UA/1.0"


def test_ytdlp_resolve_stream_rejects_video_only_format(tmp_path, monkeypatch):
    settings = base_settings(tmp_path)
    provider = YtDlpProvider(settings)

    class FakeProc:
        returncode = 0
        stdout = (
            '{"title": "v", "duration": 62.0, "url": "https://media.example/vonly.mp4", '
            '"vcodec": "avc1.64001f", "acodec": "none"}'
        )
        stderr = ""

    monkeypatch.setattr("app.downloader.ytdlp.subprocess.run", lambda *a, **kw: FakeProc())
    assert provider.resolve_stream("https://youtu.be/aBcDeFgHiJk", 0, 600) is None


def test_ytdlp_resolve_stream_failure_returns_none(tmp_path, monkeypatch):
    settings = base_settings(tmp_path)
    provider = YtDlpProvider(settings)

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "ERROR: Sign in to confirm you are not a bot"

    monkeypatch.setattr("app.downloader.ytdlp.subprocess.run", lambda *a, **kw: FakeProc())
    assert provider.resolve_stream("https://youtu.be/aBcDeFgHiJk", 0, 600) is None


# ---------------- registry & chain fallback ----------------

def test_registry_knows_all_shipped_providers():
    assert set(PROVIDERS) == {"cobalt", "ytdlp", "sample"}


def test_build_chain_order_and_unknown_provider(tmp_path):
    settings = base_settings(tmp_path, downloader_providers="ytdlp,cobalt")
    chain = build_provider_chain(settings)
    assert [p.name for p in chain] == ["ytdlp", "cobalt"]

    bad = base_settings(tmp_path, downloader_providers="doesnotexist")
    with pytest.raises(ValueError):
        build_provider_chain(bad)


def test_chain_falls_through_to_next_provider(tmp_path):
    settings = base_settings(tmp_path, max_video_height=240)

    class FailingProvider(DownloaderProvider):
        name = "failing"

        def get_video(self, url, start, end, progress=None):
            raise ProviderError("simulated outage", provider=self.name)

    providers = [FailingProvider(), SampleProvider(settings)]
    source = _download_with_chain(providers, "https://youtu.be/aBcDeFgHiJk", 0, 3)
    assert source.provider == "sample"
    source.path.unlink()


def test_chain_all_failed_collects_every_reason(tmp_path):
    settings = base_settings(tmp_path)

    class FailingProvider(DownloaderProvider):
        name = "failing"

        def get_video(self, url, start, end, progress=None):
            raise ProviderError("simulated outage", provider=self.name)

    class BrokenProvider(DownloaderProvider):
        name = "broken"

        def get_video(self, url, start, end, progress=None):
            raise RuntimeError("kaboom")

    with pytest.raises(ProviderError) as exc:
        _download_with_chain(
            [FailingProvider(), BrokenProvider()],
            "https://youtu.be/aBcDeFgHiJk",
            0,
            3,
        )
    message = exc.value.message
    assert "All download providers failed" in message
    assert "[failing] simulated outage" in message
    assert "[broken] unexpected error" in message
