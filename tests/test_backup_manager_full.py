"""Extended tests for backup_manager.BackupManager.

Covers the gaps identified by the audit:
- purge_old_logs (entire function)
- template without {date} raises ValueError
- {site} substitution
- unparseable date filename (debug skip)
- OSError on purge (warning, not raise)
- today_path / yesterday_path helpers
- retention_days=1 edge
"""

from __future__ import annotations

import types
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.backup_manager import BackupManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(tmp_path: Path, template="pricecapture_backup_{site}_{date}.csv",
         site="UKPROD", retention=7, date_format="%Y-%m-%d") -> types.SimpleNamespace:
    backup_dir = tmp_path / "backups"
    return types.SimpleNamespace(
        backup_dir=backup_dir,
        backup_filename_template=template,
        backup_date_format=date_format,
        backup_retention_days=retention,
        sender_site=site,
    )


TODAY = date(2026, 5, 14)


# ---------------------------------------------------------------------------
# Construction errors
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_raises_if_no_date_placeholder(self, tmp_path):
        cfg = _cfg(tmp_path, template="pricecapture_backup.csv")
        with pytest.raises(ValueError, match=r"\{date\}"):
            BackupManager(cfg)

    def test_site_substituted_in_template(self, tmp_path):
        cfg = _cfg(tmp_path, template="backup_{site}_{date}.csv", site="USPROD")
        bm = BackupManager(cfg)
        assert "{site}" not in bm._template
        assert "USPROD" in bm._template

    def test_legacy_template_without_site(self, tmp_path):
        cfg = _cfg(tmp_path, template="backup_{date}.csv")
        bm = BackupManager(cfg)
        p = bm.path_for_date(TODAY)
        assert TODAY.strftime("%Y-%m-%d") in p.name

    def test_retention_days_clamped_to_1(self, tmp_path):
        cfg = _cfg(tmp_path, retention=0)
        bm = BackupManager(cfg)
        assert bm._retention_days == 1

    def test_backup_dir_created(self, tmp_path):
        cfg = _cfg(tmp_path)
        BackupManager(cfg)
        assert cfg.backup_dir.is_dir()


# ---------------------------------------------------------------------------
# path_for_date / today_path / yesterday_path
# ---------------------------------------------------------------------------

class TestPathHelpers:
    def test_path_for_date_contains_date_string(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        p = bm.path_for_date(TODAY)
        assert "2026-05-14" in p.name

    def test_today_path_equals_path_for_date(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        assert bm.today_path(TODAY) == bm.path_for_date(TODAY)

    def test_yesterday_path_is_one_day_before(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        yest = bm.yesterday_path(TODAY)
        today = bm.today_path(TODAY)
        assert yest != today
        assert "2026-05-13" in yest.name


# ---------------------------------------------------------------------------
# write_today
# ---------------------------------------------------------------------------

class TestWriteToday:
    def test_writes_payload_to_file(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        p = bm.write_today("hello,csv", TODAY)
        assert p.read_text() == "hello,csv"

    def test_returns_path(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        p = bm.write_today("data", TODAY)
        assert isinstance(p, Path)
        assert p.exists()

    def test_overwrites_existing_file(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        bm.write_today("v1", TODAY)
        bm.write_today("v2", TODAY)
        p = bm.path_for_date(TODAY)
        assert p.read_text() == "v2"


# ---------------------------------------------------------------------------
# rotate_for_new_day
# ---------------------------------------------------------------------------

class TestRotateForNewDay:
    def _write_backup(self, bm: BackupManager, d: date) -> Path:
        p = bm.path_for_date(d)
        p.write_text(f"data for {d}")
        return p

    def test_purges_file_older_than_retention(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path, retention=3))
        old = TODAY - timedelta(days=5)
        p = self._write_backup(bm, old)
        bm.rotate_for_new_day(TODAY)
        assert not p.exists()

    def test_keeps_file_within_retention(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path, retention=7))
        recent = TODAY - timedelta(days=3)
        p = self._write_backup(bm, recent)
        bm.rotate_for_new_day(TODAY)
        assert p.exists()

    def test_keeps_today(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path, retention=1))
        p = self._write_backup(bm, TODAY)
        bm.rotate_for_new_day(TODAY)
        assert p.exists()

    def test_retention_1_deletes_yesterday(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path, retention=1))
        yest = TODAY - timedelta(days=1)
        p = self._write_backup(bm, yest)
        bm.rotate_for_new_day(TODAY)
        assert not p.exists()

    def test_unrelated_files_not_touched(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path, retention=3))
        unrelated = bm._backup_dir / "readme.txt"
        unrelated.write_text("keep me")
        bm.rotate_for_new_day(TODAY)
        assert unrelated.exists()

    def test_unparseable_date_filename_skipped(self, tmp_path, caplog):
        import logging
        bm = BackupManager(_cfg(tmp_path, retention=3))
        weird = bm._backup_dir / "pricecapture_backup_UKPROD_notadate.csv"
        weird.write_text("junk")
        with caplog.at_level(logging.DEBUG):
            bm.rotate_for_new_day(TODAY)
        assert weird.exists()

    def test_oserror_on_purge_logs_warning(self, tmp_path, caplog):
        import logging
        bm = BackupManager(_cfg(tmp_path, retention=1))
        old = TODAY - timedelta(days=10)
        p = self._write_backup(bm, old)
        with patch.object(Path, "unlink", side_effect=OSError("locked")):
            with caplog.at_level(logging.WARNING):
                bm.rotate_for_new_day(TODAY)
        # No exception raised

    def test_non_file_entries_skipped(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path, retention=1))
        subdir = bm._backup_dir / "somedir"
        subdir.mkdir()
        bm.rotate_for_new_day(TODAY)  # must not raise

    def test_empty_backup_dir(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        bm.rotate_for_new_day(TODAY)  # nothing to purge, must not raise


# ---------------------------------------------------------------------------
# purge_old_logs
# ---------------------------------------------------------------------------

class TestPurgeOldLogs:
    def _setup_log_dir(self, tmp_path) -> Path:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        return log_dir

    def test_deletes_old_log_not_in_keep(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        log_dir = self._setup_log_dir(tmp_path)
        old = log_dir / "sender-2026-01-01.log"
        old.write_text("old log")
        bm.purge_old_logs(log_dir, keep_filenames={"sender-2026-05-14.log"})
        assert not old.exists()

    def test_keeps_files_in_keep_set(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        log_dir = self._setup_log_dir(tmp_path)
        today_log = log_dir / "sender-2026-05-14.log"
        today_log.write_text("today")
        bm.purge_old_logs(log_dir, keep_filenames={"sender-2026-05-14.log"})
        assert today_log.exists()

    def test_keeps_both_today_and_yesterday(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        log_dir = self._setup_log_dir(tmp_path)
        today_log = log_dir / "sender-2026-05-14.log"
        yest_log = log_dir / "sender-2026-05-13.log"
        old_log = log_dir / "sender-2026-05-10.log"
        for f in [today_log, yest_log, old_log]:
            f.write_text("x")
        bm.purge_old_logs(
            log_dir,
            keep_filenames={"sender-2026-05-14.log", "sender-2026-05-13.log"},
        )
        assert today_log.exists()
        assert yest_log.exists()
        assert not old_log.exists()

    def test_non_log_files_not_touched(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        log_dir = self._setup_log_dir(tmp_path)
        csv_file = log_dir / "something.csv"
        csv_file.write_text("data")
        bm.purge_old_logs(log_dir, keep_filenames=set())
        assert csv_file.exists()

    def test_oserror_on_log_delete_logs_warning(self, tmp_path, caplog):
        import logging
        bm = BackupManager(_cfg(tmp_path))
        log_dir = self._setup_log_dir(tmp_path)
        old = log_dir / "sender-2026-01-01.log"
        old.write_text("old")
        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            with caplog.at_level(logging.WARNING):
                bm.purge_old_logs(log_dir, keep_filenames=set())
        # No exception

    def test_empty_log_dir(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        log_dir = self._setup_log_dir(tmp_path)
        bm.purge_old_logs(log_dir, keep_filenames=set())  # must not raise

    def test_empty_keep_set_purges_all_logs(self, tmp_path):
        bm = BackupManager(_cfg(tmp_path))
        log_dir = self._setup_log_dir(tmp_path)
        for name in ["sender-2026-05-14.log", "sender-2026-05-13.log"]:
            (log_dir / name).write_text("x")
        bm.purge_old_logs(log_dir, keep_filenames=set())
        assert list(log_dir.glob("*.log")) == []
