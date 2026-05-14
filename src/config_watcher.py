"""Config-file change watcher.

Runs a background daemon thread that polls the mtime of the three config
files every CONFIG_WATCH_INTERVAL_SECONDS (default 10 s).  If any file
has been modified since the process started, it logs a clear message and
calls os._exit(0).

Why os._exit(0)?
    * Same pattern as the heartbeat watchdog (os._exit(99)).
    * Task Scheduler / NSSM sees the process exit and relaunches it.
    * The new process calls load_config() from scratch, picking up every
      change atomically.  There is no in-process hot-reload; the whole
      config is frozen at startup by design.
    * Exit code 0 ("clean restart due to config change") is distinguishable
      from code 99 (heartbeat timeout) and 1 (unhandled exception).

Files watched:
    1. .env
    2. config/config.json
    3. config/price_groups.json

All three paths are resolved relative to the project root at the time
start() is called, so they are correct even when the process working
directory differs from the project root.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .logger_setup import get_logger

log = get_logger("config_watcher")

# Polling interval (seconds).  10 s keeps restart latency low while
# generating negligible I/O.
_DEFAULT_INTERVAL = 10.0


class ConfigWatcher:
    """Background daemon that triggers a clean restart on any config change."""

    def __init__(
        self,
        watched_paths: list[Path],
        interval_seconds: float = _DEFAULT_INTERVAL,
    ) -> None:
        self._paths = [p.resolve() for p in watched_paths]
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="config-watcher",
            daemon=True,
        )
        # Record baseline mtimes at construction time (= process start).
        self._baseline: dict[Path, float] = {}
        for p in self._paths:
            try:
                self._baseline[p] = p.stat().st_mtime
            except OSError:
                # File doesn't exist yet (unlikely for config files but
                # tolerate gracefully — it will be caught on the next poll).
                self._baseline[p] = 0.0
        log.info(
            "Config watcher armed | watching=%d files | interval=%gs",
            len(self._paths), self._interval,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Signal the watcher to stop (called on graceful SIGINT/SIGTERM)."""
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            changed = self._detect_changes()
            if changed:
                for path in changed:
                    log.warning(
                        "Config file changed — scheduling clean restart | file=%s",
                        path,
                    )
                log.warning(
                    "=== CONFIG CHANGE DETECTED — restarting process (exit 0) ==="
                    " Task Scheduler / NSSM will relaunch with the new config. ==="
                )
                # Give the warning a moment to flush to the log file.
                time.sleep(0.5)
                os._exit(0)

    def _detect_changes(self) -> list[Path]:
        changed: list[Path] = []
        for p in self._paths:
            try:
                current_mtime = p.stat().st_mtime
            except OSError:
                # File was deleted — treat as a change so the process
                # restarts and load_config() raises a clear error.
                if self._baseline.get(p, 0.0) != 0.0:
                    changed.append(p)
                continue
            if current_mtime != self._baseline.get(p, current_mtime):
                changed.append(p)
        return changed
