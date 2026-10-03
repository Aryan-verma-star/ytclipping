"""Worker supervisor — keeps the in-process worker threads alive.

Incident (2026-10-03, production): the single preview-worker thread hung
inside a provider download; every later preview queued behind it at
``pending`` and users saw an endless "downloading" spinner until someone
restarted the service. Python threads cannot be killed, so this supervisor
does the next best thing:

- it RESTARTS any managed worker thread that has died, and
- when a worker has been inside ONE work item for longer than its hard
  wall, it marks that item failed in the database (with a clear message,
  cascading to jobs waiting on a preview) and starts a REPLACEMENT worker.

The abandoned thread is left to finish on its own; this is safe because:

- every write ``process_preview``/``process_job`` might still make is
  guarded by a status re-check (they only advance items found in the
  expected status — once the supervisor failed the item, the stale writes
  are no-ops), and
- each preview/job owns its own work directory and DB rows, so a
  replacement worker never touches the abandoned item (``oldest_pending``
  / ``oldest_runnable`` skip non-pending items).

The sweeper-style hard walls remain as a backstop; this supervisor simply
reacts faster (30 s) and, crucially, restores throughput.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from app.config import Settings
from app.db.base import Database

log = logging.getLogger("clipper.supervisor")


class WorkerSupervisor(threading.Thread):
    """Watchdog that keeps worker threads alive and unstuck."""

    def __init__(self, db: Database, settings: Settings, stop_event: threading.Event) -> None:
        super().__init__(daemon=True, name="clipper-supervisor")
        self.db = db
        self.settings = settings
        self.stop_event = stop_event
        # name -> {"factory", "wall", "on_stuck", "thread", "replacements"}
        self._specs: dict[str, dict] = {}
        self._lock = threading.Lock()

    # -- registration --------------------------------------------------------

    def manage(
        self,
        name: str,
        factory: Callable[[], threading.Thread],
        *,
        wall_seconds: float | None = None,
        on_stuck: Callable[[Database, str], None] | None = None,
    ) -> threading.Thread:
        """Register, start and supervise a worker thread.

        ``factory()`` must return a NEW un-started thread each call (it is
        re-invoked for replacements). ``wall_seconds`` bounds how long ONE
        work item may run before the item is failed (via ``on_stuck``) and
        the worker replaced; ``None`` means only liveness is supervised.
        """
        thread = factory()
        thread.start()
        with self._lock:
            self._specs[name] = {
                "factory": factory,
                "wall": wall_seconds,
                "on_stuck": on_stuck,
                "thread": thread,
                "replacements": 0,
            }
        return thread

    # -- queries -------------------------------------------------------------

    def is_alive(self, name: str) -> bool | None:
        """True/False when the worker is managed; None when unknown."""
        with self._lock:
            spec = self._specs.get(name)
            return bool(spec and spec["thread"].is_alive()) if spec else None

    def replacements(self, name: str) -> int:
        with self._lock:
            spec = self._specs.get(name)
            return int(spec["replacements"]) if spec else 0

    def thread(self, name: str) -> threading.Thread | None:
        with self._lock:
            spec = self._specs.get(name)
            return spec["thread"] if spec else None

    # -- loop ----------------------------------------------------------------

    def _replace(self, name: str, spec: dict, reason: str) -> None:
        try:
            fresh = spec["factory"]()
            fresh.start()
        except Exception:  # pragma: no cover - factory failure is fatal-ish
            log.exception("supervisor could not restart %s (%s)", name, reason)
            return
        spec["thread"] = fresh
        spec["replacements"] += 1
        log.warning(
            "supervisor replaced %s (%s) — old thread abandoned, new one started",
            name,
            reason,
        )

    def run(self) -> None:
        log.info("worker supervisor started (interval=%ss)", self.settings.supervisor_interval_seconds)
        while not self.stop_event.is_set():
            try:
                self._audit()
            except Exception:  # pragma: no cover - the loop must never die
                log.exception("supervisor audit failed")
            self.stop_event.wait(self.settings.supervisor_interval_seconds)
        log.info("worker supervisor stopped")

    def _audit(self) -> None:
        with self._lock:
            for name, spec in self._specs.items():
                thread = spec["thread"]
                if not thread.is_alive():
                    self._replace(name, spec, "thread died")
                    continue
                wall = spec["wall"]
                item_id = getattr(thread, "current_id", None)
                started = getattr(thread, "current_since", None)
                if not wall or not item_id or not started:
                    continue
                elapsed = time.monotonic() - started
                if elapsed <= wall:
                    continue
                # stuck inside one item beyond the hard wall: fail the item
                # (clear message, cascades) and put a fresh worker on the queue
                log.error(
                    "%s has been processing %s for %ds (wall=%ss) — failing the "
                    "item and replacing the worker",
                    name,
                    item_id,
                    int(elapsed),
                    int(wall),
                )
                on_stuck = spec["on_stuck"]
                if on_stuck is not None:
                    try:
                        on_stuck(self.db, item_id)
                    except Exception:  # pragma: no cover
                        log.exception("on_stuck hook failed for %s %s", name, item_id)
                self._replace(name, spec, f"stuck on {item_id} for {int(elapsed)}s")

    def stop(self) -> None:
        self.stop_event.set()
