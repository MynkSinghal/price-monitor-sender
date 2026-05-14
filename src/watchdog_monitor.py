"""Real-time watchdog monitor.

Architecture (Q13):
    * One background Observer (from the `watchdog` library) watches
      every <PRICES_ROOT>/<job>/GetPricesResult/ folder for success.txt.
    * On Windows the Observer uses ReadDirectoryChangesW under the hood
      (which is why `watchdog` was chosen).
    * When success.txt appears, we capture st_ctime synchronously and
      hand off to the StateManager. First-detection-locked semantics (Q18)
      are enforced inside the state manager.
    * Startup reconciliation: we do a one-pass scan of every watched
      folder so that files created before the process started (e.g. the
      process restarted mid-day) are immediately picked up.
    * Missing folders are OK — we log and skip them (RANTask may not
      have run yet, or the job may be inactive today).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Iterable

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config_loader import AppConfig
from .logger_setup import get_logger
from .state_manager import StateManager

log = get_logger("watchdog")


class _SuccessFileHandler(FileSystemEventHandler):
    """Fires on success.txt create/move within a watched GetPricesResult dir."""

    def __init__(self, job_name: str, success_filename: str, state: StateManager) -> None:
        self._job_name = job_name
        self._target_name = success_filename.lower()
        self._state = state

    def _maybe_record(self, path: str) -> None:
        if Path(path).name.lower() != self._target_name:
            return
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            log.warning("success.txt vanished before stat() for job=%s path=%s", self._job_name, path)
            return
        except OSError as exc:
            log.error("stat() failed for %s: %s", path, exc)
            return
        self._state.record(self._job_name, float(stat.st_ctime), str(path))

    def on_created(self, event):
        if isinstance(event, FileCreatedEvent) and not event.is_directory:
            self._maybe_record(event.src_path)

    def on_moved(self, event):
        if isinstance(event, FileMovedEvent) and not event.is_directory:
            self._maybe_record(event.dest_path)


class WatchdogMonitor:
    def __init__(self, cfg: AppConfig, state: StateManager) -> None:
        self._cfg = cfg
        self._state = state
        self._observer = Observer()
        self._started = threading.Event()
        self._all_jobs: tuple[str, ...] = ()
        self._watched_jobs: set[str] = set()

    # ------------------ startup reconciliation ------------------

    def reconcile_existing_files(self, jobs: Iterable[str]) -> int:
        """Scan every active job's folder once for a pre-existing success.txt.

        Returns the number of records newly captured.
        """
        found = 0
        for job in jobs:
            path = self._cfg.resolve_job_path(job)
            try:
                if path.is_file():
                    stat = path.stat()
                    if self._state.record(job, float(stat.st_ctime), str(path)):
                        found += 1
            except OSError as exc:
                log.debug("Skipping reconcile for %s (%s)", job, exc)
        log.info("Startup reconciliation complete — %d new records captured", found)
        return found

    # ------------------ observer lifecycle ------------------

    def start(self, jobs: Iterable[str]) -> None:
        if self._started.is_set():
            return
        self._all_jobs = tuple(jobs)
        self._watched_jobs: set[str] = set()
        self._attach_watches(self._all_jobs)
        self._observer.start()
        self._started.set()
        log.info(
            "Watchdog observer started | watching=%d | missing_dirs=%d",
            len(self._watched_jobs), len(self._all_jobs) - len(self._watched_jobs),
        )

    def _attach_watches(self, jobs: Iterable[str]) -> int:
        """Attach a handler to each job's watch dir. Safe to call repeatedly.

        Jobs whose watch dir does not yet exist are skipped silently and
        picked up on the next call (done by the per-cycle reconcile sweep).
        """
        attached = 0
        for job in jobs:
            if job in self._watched_jobs:
                continue
            watch_dir = self._cfg.resolve_job_watch_dir(job)
            if not watch_dir.is_dir():
                continue
            handler = _SuccessFileHandler(
                job_name=job,
                success_filename=self._cfg.success_filename,
                state=self._state,
            )
            self._observer.schedule(handler, str(watch_dir), recursive=False)
            self._watched_jobs.add(job)
            attached += 1
        return attached

    def reconcile_and_watch(self) -> tuple[int, int]:
        """Cheap per-cycle sweep: catch late-created dirs AND late success.txt
        files the observer may have missed.

        Returns (new_watches, new_records).
        """
        new_records = self.reconcile_existing_files(self._all_jobs)
        new_watches = self._attach_watches(self._all_jobs)
        if new_watches or new_records:
            log.info(
                "Reconcile sweep | new_watches=%d | new_records=%d",
                new_watches, new_records,
            )
        return new_watches, new_records

    def stop(self, timeout: float = 5.0) -> None:
        if not self._started.is_set():
            return
        self._observer.stop()
        self._observer.join(timeout=timeout)
        log.info("Watchdog observer stopped")
