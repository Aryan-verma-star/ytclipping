"""WorkerSupervisor tests — all offline, no real waiting.

Covers the two heals that fix the 2026-10-03 production incident:
- a worker thread that DIES is restarted by the next audit;
- a worker stuck inside ONE item beyond its wall gets that item failed
  (on_stuck hook) and the worker thread replaced.
"""

from __future__ import annotations

import threading
import time

from app.config import Settings
from app.db.base import Database
from app.services.supervisor import WorkerSupervisor


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


class _ExitImmediately(threading.Thread):
    """Simulates a crashed worker: run() returns without looping."""

    def __init__(self):
        super().__init__(daemon=True)
        self.current_id = None
        self.current_since = None

    def run(self):  # pragma: no cover - trivially exits
        pass


class _HealthyWorker(threading.Thread):
    """Idles on the stop event like the real workers do."""

    def __init__(self, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.current_id = None
        self.current_since = None
        self._stop_event = stop_event

    def run(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(0.05)


class _StuckWorker(threading.Thread):
    """Sets current_id/current_since then blocks forever — the hung worker."""

    def __init__(self, stop_event: threading.Event, item_id: str = "item-1"):
        super().__init__(daemon=True)
        self.current_id = item_id
        self.current_since = time.monotonic() - 999.0  # already ancient
        self._stop_event = stop_event

    def run(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(0.05)


def _db(settings: Settings) -> Database:
    return Database(settings.resolved_database_url)


def test_dead_worker_is_replaced(tmp_path):
    settings = base_settings(tmp_path)
    db = _db(settings)
    stop = threading.Event()
    sup = WorkerSupervisor(db, settings, stop)

    made = []

    def factory():
        made.append(1)
        # first thread dies instantly; later ones are healthy
        return _ExitImmediately() if len(made) == 1 else _HealthyWorker(stop)

    sup.manage("job_worker", factory, wall_seconds=60.0)
    assert sup.is_alive("job_worker") is False  # it exited already

    sup._audit()  # what the run() loop does every interval
    assert sup.replacements("job_worker") == 1
    assert sup.is_alive("job_worker") is True
    assert len(made) == 2

    stop.set()


def test_stuck_worker_fails_item_and_is_replaced(tmp_path):
    settings = base_settings(tmp_path)
    db = _db(settings)
    stop = threading.Event()
    sup = WorkerSupervisor(db, settings, stop)

    stuck_calls: list[str] = []
    made: list[list] = []

    def factory():
        made.append(1)
        return _StuckWorker(stop, item_id="preview-42")

    def on_stuck(db, item_id):
        stuck_calls.append(item_id)

    sup.manage("preview_worker", factory, wall_seconds=10.0, on_stuck=on_stuck)
    assert sup.is_alive("preview_worker") is True

    sup._audit()
    assert stuck_calls == ["preview-42"]
    assert sup.replacements("preview_worker") == 1
    # replacement is a fresh stuck-shaped thread (factory re-invoked)
    assert len(made) == 2

    stop.set()


def test_healthy_worker_is_left_alone(tmp_path):
    settings = base_settings(tmp_path)
    db = _db(settings)
    stop = threading.Event()
    sup = WorkerSupervisor(db, settings, stop)

    def on_stuck(db, item_id):  # pragma: no cover - must not fire
        raise AssertionError("healthy worker must not be failed")

    sup.manage(
        "preview_worker",
        lambda: _HealthyWorker(stop),
        wall_seconds=10.0,
        on_stuck=on_stuck,
    )
    sup._audit()
    sup._audit()
    assert sup.replacements("preview_worker") == 0
    assert sup.is_alive("preview_worker") is True

    stop.set()


def test_unknown_name_queries(tmp_path):
    settings = base_settings(tmp_path)
    db = _db(settings)
    stop = threading.Event()
    sup = WorkerSupervisor(db, settings, stop)
    assert sup.is_alive("nope") is None
    assert sup.replacements("nope") == 0
    assert sup.thread("nope") is None
    stop.set()
