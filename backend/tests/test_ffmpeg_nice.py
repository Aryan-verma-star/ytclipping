"""ffmpeg/ffprobe subprocesses must run at low priority.

On Render's free tier a foreground ffmpeg saturating the throttled CPU kept
the web process from answering Render's edge within its timeout window —
the site 502'd for minutes while clips rendered (reproduced in production:
job completed server-side while every poll 502'd). All heavy binaries are
therefore exec'd through `nice -n 10` whenever coreutils is available.
"""

from __future__ import annotations

import app.core.ffmpeg as ffmpeg_mod


class _FakeProc:
    returncode = 0
    stdout = '{"format": {}, "streams": []}'
    stderr = ""


def _install_capture(monkeypatch):
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ARG001
        seen.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", fake_run)
    return seen


def test_run_ffmpeg_uses_nice_when_available(monkeypatch):
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda name: "/usr/bin/nice")
    monkeypatch.setattr(ffmpeg_mod, "_NICE_PREFIX", None)
    seen = _install_capture(monkeypatch)
    ffmpeg_mod.run_ffmpeg(["-version"])
    assert seen[0][:3] == ["nice", "-n", "10"]
    assert "ffmpeg" in seen[0][3]


def test_run_ffmpeg_without_nice_still_works(monkeypatch):
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(ffmpeg_mod, "_NICE_PREFIX", None)
    seen = _install_capture(monkeypatch)
    ffmpeg_mod.run_ffmpeg(["-version"])
    assert seen[0][0] == ffmpeg_mod.FFMPEG_BIN  # no prefix, binary first


def test_ffprobe_json_uses_nice_when_available(monkeypatch):
    monkeypatch.setattr(ffmpeg_mod.shutil, "which", lambda name: "/usr/bin/nice")
    monkeypatch.setattr(ffmpeg_mod, "_NICE_PREFIX", None)
    seen = _install_capture(monkeypatch)
    ffmpeg_mod.ffprobe_json("/tmp/x.mp4")
    assert seen[0][:3] == ["nice", "-n", "10"]
    assert "ffprobe" in seen[0][3]


def test_nice_prefix_is_cached(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        ffmpeg_mod.shutil, "which", lambda name: calls.append(name) or "/usr/bin/nice"
    )
    monkeypatch.setattr(ffmpeg_mod, "_NICE_PREFIX", None)
    assert ffmpeg_mod.nice_prefix() == ["nice", "-n", "10"]
    assert ffmpeg_mod.nice_prefix() == ["nice", "-n", "10"]
    assert calls == ["nice"]  # which() consulted exactly once
