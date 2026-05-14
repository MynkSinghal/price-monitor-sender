"""Self-kill watchdog for the main loop (Q17).

Model:
    * Main loop calls heartbeat.tick() at the start of every cycle.
    * A background daemon thread wakes periodically; if the time since
      the last tick exceeds HEARTBEAT_MAX_SILENCE_SECONDS the thread
      logs a fatal line and os._exit()s the process.
    * Windows Task Scheduler then relaunches the process on its next
      scheduled check (the task is configured with 'If the task is
      already running, do not start a new instance' so the relaunch
      waits until the old one has fully exited).

We use os._exit() rather than sys.exit() deliberately — the point is
to bypass Python's finalisation in case one of the threads is wedged
in a C extension.
"""

from __future__ import annotations

import os
import threading
import time

from .logger_setup import get_logger

log = get_logger("heartbeat")


class Heartbeat:
    def __init__(self, max_silence_seconds: int, check_interval_seconds: float = 5.0) -> None:
        self._max_silence = max_silence_seconds
        self._check_interval = check_interval_seconds
        self._last_tick = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()
        log.info("Heartbeat watchdog started | max_silence=%ds", self._max_silence)

    def tick(self) -> None:
        with self._lock:
            self._last_tick = time.monotonic()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._check_interval):
            with self._lock:
                silent_for = time.monotonic() - self._last_tick
            if silent_for > self._max_silence:
                log.critical(
                    "Main loop silent for %.1fs (> %ds) — triggering self-kill. "
                    "Task Scheduler will relaunch the process.",
                    silent_for, self._max_silence,
                )
                os._exit(99)
