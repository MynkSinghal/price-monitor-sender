"""Price Monitor Sender — entry point.

High-level execution order (see docs/EXECUTION_ORDER.md for the full diagram):
    1. Load config (.env + config.json + price_groups.json).
    2. Set up rotating daily logging.
    3. Build StateManager; load any prior state.json left by a crashed process.
    4. Run daily-reset if the wall clock says it is a fresh day since state's saved_at.
    5. Build BackupManager; rotate backups if needed.
    6. Start WatchdogMonitor observer; run startup reconciliation sweep.
    7. Start Heartbeat self-kill watchdog.
    8. Start ConfigWatcher — polls .env / config.json / price_groups.json every 10 s;
       calls os._exit(0) on any change so Task Scheduler / NSSM relaunches the process.
    9. Enter the main send loop (runs forever until SIGINT / self-kill):
         a. heartbeat.tick()
         b. compute clock, check weekday mask, check whether we should sleep
         c. if daily reset window fired → perform_daily_reset()
         d. build CSV snapshot, write pricecapture_backup_<today>.csv, POST to receiver
         e. sleep until next cycle per scheduler.interval_seconds()
   10. On Ctrl-C: graceful shutdown — stop observer, stop watcher, stop transmitter,
       persist one final state.json, flush logs, exit 0.
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import date
from pathlib import Path
from threading import Event
from uuid import uuid4

from .backup_manager import BackupManager
from .config_loader import AppConfig, PROJECT_ROOT, load_config
from .config_watcher import ConfigWatcher
from .csv_builder import CsvBuilder
from .daily_reset import perform_daily_reset
from .heartbeat import Heartbeat
from .logger_setup import get_logger, setup_logging
from .scheduler import Scheduler, now as clock_now, business_day_start_epoch_for
from .state_manager import StateManager
from .transmitter import Transmitter
from .watchdog_monitor import WatchdogMonitor


class Sender:
    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._log = get_logger("main")
        self._state = StateManager(
            state_file=cfg.state_dir / "state.json",
            first_detection_locked=cfg.first_detection_locked,
            business_tz=cfg.timezone_business,
            stale_cutoff_provider=lambda: business_day_start_epoch_for(cfg, clock_now(cfg).business_day),
        )
        self._backup = BackupManager(cfg)
        self._csv = CsvBuilder(cfg, self._state)
        self._tx = Transmitter(cfg)
        self._sched = Scheduler(cfg)
        self._monitor = WatchdogMonitor(cfg, self._state)
        self._heart = Heartbeat(cfg.heartbeat_max_silence_seconds)
        self._config_watcher = ConfigWatcher([
            PROJECT_ROOT / ".env",
            cfg.config_dir / "config.json",
            cfg.config_dir / "price_groups.json",
        ])
        self._stop = Event()
        self._last_reset_date: date | None = None

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):
            self._log.info("Signal %s received — initiating graceful shutdown", signum)
            self._stop.set()
        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, AttributeError):
            pass

    def start(self) -> int:
        self._log.info(
            "=== PRICE MONITOR SENDER START | site=%s | env=%s | host=%s | mode=%s | price_groups=%d ===",
            self._cfg.sender_site, self._cfg.sender_env, self._cfg.sender_hostname,
            self._cfg.send_mode, len(self._cfg.active_price_groups),
        )
        self._log_site_summary()

        self._install_signal_handlers()

        initial_clock = clock_now(self._cfg)
        perform_daily_reset(self._cfg, self._state, self._backup, initial_clock.business_day)
        self._last_reset_date = initial_clock.business_day

        cross_site = self._cfg.cross_site_jobs
        all_jobs = [j for pg in self._cfg.active_price_groups for j in pg.jobs]
        local_jobs = [j for j in all_jobs if j not in cross_site]
        if cross_site:
            self._log.info(
                "site=%s | skipping watcher attach for %d cross-site job(s) (paths live on the OTHER host)",
                self._cfg.sender_site, len(set(all_jobs) & cross_site),
            )
        self._monitor.reconcile_existing_files(local_jobs)
        self._monitor.start(local_jobs)
        self._heart.start()
        self._config_watcher.start()

        exit_code = 0
        try:
            self._run_loop()
        except Exception:
            self._log.exception("Fatal error in main loop — exiting with code 1")
            exit_code = 1
        finally:
            self._shutdown()
        return exit_code

    def _run_loop(self) -> None:
        cfg = self._cfg
        while not self._stop.is_set():
            self._heart.tick()
            clock = clock_now(cfg)

            if not self._sched.should_run_today(clock):
                self._log.debug(
                    "Inactive weekday (%s, business_day=%s) — sleeping 60s",
                    clock.business.strftime("%A"), clock.business_day.isoformat(),
                )
                self._stop.wait(60)
                continue

            if self._sched.is_daily_reset_window(clock, self._last_reset_date):
                perform_daily_reset(cfg, self._state, self._backup, clock.business_day)
                self._last_reset_date = clock.business_day

            self._do_one_cycle(cycle_id=uuid4().hex[:12], today=clock.business_day)

            interval = self._sched.interval_seconds(clock)
            self._log.debug(
                "Cycle complete | next_in=%ds | business=%s (%s) | business_day=%s | system=%s",
                interval,
                clock.business.strftime("%H:%M"),
                cfg.timezone_business,
                clock.business_day.isoformat(),
                clock.system.strftime("%H:%M %Z"),
            )
            self._stop.wait(interval)

    def _do_one_cycle(self, *, cycle_id: str, today: date) -> None:
        self._monitor.reconcile_and_watch()
        snapshot = self._csv.build(self._cfg.active_price_groups)
        self._backup.write_today(snapshot.payload, today)
        result = self._tx.send(snapshot.payload, cycle_id=cycle_id)
        self._log.info(
            "CYCLE %s | site=%s | rows=%d | complete=%d | pending=%d | tx_ok=%s | bytes=%d | elapsed_ms=%.0f",
            cycle_id, self._cfg.sender_site, len(snapshot.rows),
            snapshot.complete_count, snapshot.pending_count,
            result.ok, result.bytes_sent, result.elapsed_ms,
        )

    def _log_site_summary(self) -> None:
        """One-shot startup line counting always-empty rows for this site."""
        cross_site = self._cfg.cross_site_jobs
        if not cross_site:
            return
        always_empty = 0
        partial = 0
        for pg in self._cfg.active_price_groups:
            jobset = set(pg.jobs)
            if jobset and jobset.issubset(cross_site):
                always_empty += 1
            elif jobset & cross_site:
                partial += 1
        self._log.info(
            "site=%s | always-empty rows on this host: %d (pure cross-site composites: %d) | "
            "the receiver fills these from the other site's CSV",
            self._cfg.sender_site, always_empty, partial,
        )

    def _shutdown(self) -> None:
        self._log.info("Graceful shutdown begin")
        self._config_watcher.stop()
        self._heart.stop()
        self._monitor.stop()
        self._tx.close()
        self._log.info("Graceful shutdown complete")


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    setup_logging(cfg)

    missing_receiver = not cfg.receiver_url or cfg.receiver_url.startswith("http://CHANGE_ME")
    if missing_receiver:
        get_logger("main").critical("RECEIVER_URL is not configured. Edit .env and restart.")
        return 2

    sender = Sender(cfg)
    return sender.start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
