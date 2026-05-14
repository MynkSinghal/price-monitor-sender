"""Daily reset routine.

Fires whenever the business day rolls over. A "business day" is the
~24h window starting at ``BUSINESS_DAY_START`` (default 06:00) in
``BUSINESS_TIMEZONE`` (default Europe/London, DST/BST aware). On a host
running on US system time, the rollover still fires at 06:00 UK.

Responsibilities of a reset:
    1. Clear in-memory state + state.json.
    2. Purge backup files outside the retention window
       (filenames are business-day-date-stamped, so no rename is needed).
    3. Purge log files older than yesterday (Q12).

This is an idempotent function. It is called:
    * Once at startup (to align the sender with whichever business day
      it woke up in).
    * Every loop iteration where scheduler.is_daily_reset_window() flips
      true (i.e. the business day differs from the last reset).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from .backup_manager import BackupManager
from .config_loader import AppConfig
from .logger_setup import get_logger, rotate_log_file
from .state_manager import StateManager

log = get_logger("reset")


def perform_daily_reset(
    cfg: AppConfig,
    state: StateManager,
    backup: BackupManager,
    today: date,
) -> None:
    log.info("=== DAILY RESET BEGIN | target_date=%s ===", today.isoformat())

    # Rotate the log file FIRST so every line from this point on lands in
    # the new business day's file. The purge step below will then correctly
    # leave today's (new) file and yesterday's file, and delete everything older.
    rotate_log_file(cfg, today)

    state.clear()

    # Remove any .state-*.json.tmp orphans left by a hard os._exit() mid-write
    # (config-watcher restart or power cut while persisting state).
    for orphan in cfg.state_dir.glob(".state-*.json.tmp"):
        try:
            orphan.unlink()
            log.debug("Removed orphaned state temp file: %s", orphan.name)
        except OSError as exc:
            log.warning("Could not remove orphaned state temp file %s: %s", orphan.name, exc)

    backup.rotate_for_new_day(today)

    keep_today = today.strftime(cfg.log_filename_pattern)
    keep_yesterday = (today - timedelta(days=1)).strftime(cfg.log_filename_pattern)
    backup.purge_old_logs(Path(cfg.log_dir), keep_filenames={keep_today, keep_yesterday})

    log.info("=== DAILY RESET COMPLETE ===")
