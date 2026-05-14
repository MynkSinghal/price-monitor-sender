"""CSV backup management — date-stamped, site-tagged filenames.

Naming scheme (configured in config.json → backup.filename_template):
    pricecapture_backup_{site}_{date}.csv    ← recommended (site-tagged)
    pricecapture_backup_{date}.csv           ← legacy (no site tag)

Examples with the site-tagged template and SENDER_SITE=UKPROD:
    pricecapture_backup_UKPROD_2026-04-23.csv  ← today's snapshot (rewritten every cycle)
    pricecapture_backup_UKPROD_2026-04-22.csv  ← yesterday's final snapshot (frozen)
    pricecapture_backup_UKPROD_<older>.csv     ← purged at the next daily reset

The {site} placeholder is optional — omit it for the legacy single-site template.
When present it is replaced with AppConfig.sender_site (UKPROD or USPROD) so that
backups from both machines can be collected into a single folder without overwriting
each other.

Design notes:
    * write_today(payload, today) overwrites the file whose date-part == today.
    * rotate_for_new_day(today) is idempotent and safe to call at startup.
      It deletes any backup file whose embedded date is older than
      (today - retention_days + 1). Nothing to rename — the filename
      itself records which day the snapshot belonged to.
    * Only files matching the template prefix/suffix are considered;
      unrelated *.csv files are never touched.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .config_loader import AppConfig
from .logger_setup import get_logger

log = get_logger("backup")


class BackupManager:
    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._backup_dir = cfg.backup_dir
        self._backup_dir.mkdir(parents=True, exist_ok=True)

        # Resolve {site} placeholder first (optional), then keep {date} for per-day naming.
        raw_template = cfg.backup_filename_template
        self._template = raw_template.replace("{site}", cfg.sender_site)

        if "{date}" not in self._template:
            raise ValueError(
                f"backup filename_template must contain '{{date}}' placeholder, got {raw_template!r}"
            )
        self._date_format = cfg.backup_date_format
        self._retention_days = max(1, cfg.backup_retention_days)

        self._prefix, self._suffix = self._template.split("{date}", 1)
        date_regex = re.escape(self._prefix) + r"(?P<date>.+?)" + re.escape(self._suffix) + r"$"
        self._filename_re = re.compile(date_regex)

    def path_for_date(self, d: date) -> Path:
        filename = self._template.format(date=d.strftime(self._date_format))
        return self._backup_dir / filename

    def today_path(self, today: date) -> Path:
        return self.path_for_date(today)

    def yesterday_path(self, today: date) -> Path:
        return self.path_for_date(today - timedelta(days=1))

    def write_today(self, payload: str, today: date) -> Path:
        out = self.path_for_date(today)
        out.write_text(payload, encoding="utf-8")
        return out

    def rotate_for_new_day(self, today: date) -> None:
        """Purge backups older than retention_days.

        We keep today and the previous (retention_days - 1) days. Everything
        else matching the template is deleted.
        """
        keep_from = today - timedelta(days=self._retention_days - 1)
        purged = 0
        for child in sorted(self._backup_dir.iterdir()):
            if not child.is_file():
                continue
            m = self._filename_re.match(child.name)
            if not m:
                continue
            try:
                file_date = datetime.strptime(m.group("date"), self._date_format).date()
            except ValueError:
                log.debug("Skipping file with unparseable date: %s", child.name)
                continue
            if file_date < keep_from:
                try:
                    child.unlink()
                    log.info("Purged old backup: %s (date=%s)", child.name, file_date.isoformat())
                    purged += 1
                except OSError as exc:
                    log.warning("Could not purge %s: %s", child, exc)
        if purged:
            log.info(
                "Backup rotation complete | purged=%d | keeping dates >= %s",
                purged, keep_from.isoformat(),
            )

    def purge_old_logs(self, log_dir: Path, keep_filenames: set[str]) -> None:
        """Delete any log files not in keep_filenames (Q12: retain yesterday only)."""
        for child in log_dir.iterdir():
            if not child.is_file() or child.suffix.lower() != ".log":
                continue
            if child.name in keep_filenames:
                continue
            try:
                child.unlink()
                log.info("Purged old log: %s", child)
            except OSError as exc:
                log.warning("Could not purge %s: %s", child, exc)
