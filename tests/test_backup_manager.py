from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from src.backup_manager import BackupManager


def _cfg(tmp_path: Path, retention: int = 2):
    return SimpleNamespace(
        backup_dir=tmp_path / "backups",
        backup_filename_template="pricecapture_backup_{date}.csv",
        backup_date_format="%Y-%m-%d",
        backup_retention_days=retention,
        sender_site="UKPROD",
    )


def test_write_today_creates_date_stamped_file(tmp_path: Path) -> None:
    mgr = BackupManager(_cfg(tmp_path))
    today = date(2026, 4, 23)
    mgr.write_today("day content", today)
    expected = tmp_path / "backups" / "pricecapture_backup_2026-04-23.csv"
    assert expected.read_text() == "day content"


def test_rotation_purges_files_older_than_retention(tmp_path: Path) -> None:
    mgr = BackupManager(_cfg(tmp_path, retention=2))
    today = date(2026, 4, 23)
    yesterday = today - timedelta(days=1)
    day_before = today - timedelta(days=2)
    week_ago = today - timedelta(days=7)

    mgr.write_today("today", today)
    mgr.write_today("yesterday", yesterday)
    mgr.write_today("day-before", day_before)
    mgr.write_today("ancient", week_ago)

    mgr.rotate_for_new_day(today)

    assert mgr.path_for_date(today).exists()
    assert mgr.path_for_date(yesterday).exists()
    assert not mgr.path_for_date(day_before).exists()
    assert not mgr.path_for_date(week_ago).exists()


def test_rotation_keeps_today_and_yesterday_only_by_default(tmp_path: Path) -> None:
    mgr = BackupManager(_cfg(tmp_path, retention=2))
    today = date(2026, 4, 23)
    mgr.write_today("t", today)
    mgr.write_today("y", today - timedelta(days=1))
    mgr.rotate_for_new_day(today)

    remaining = sorted(p.name for p in (tmp_path / "backups").iterdir())
    assert remaining == [
        "pricecapture_backup_2026-04-22.csv",
        "pricecapture_backup_2026-04-23.csv",
    ]


def test_rotation_ignores_unrelated_files(tmp_path: Path) -> None:
    mgr = BackupManager(_cfg(tmp_path, retention=2))
    backup_dir = tmp_path / "backups"
    (backup_dir / "random.csv").write_text("not ours")
    (backup_dir / "pricecapture_backup_not-a-date.csv").write_text("bogus date")

    today = date(2026, 4, 23)
    mgr.write_today("t", today)
    mgr.rotate_for_new_day(today)

    assert (backup_dir / "random.csv").exists()
    assert (backup_dir / "pricecapture_backup_not-a-date.csv").exists()


def test_rotation_is_idempotent_with_only_today(tmp_path: Path) -> None:
    mgr = BackupManager(_cfg(tmp_path))
    today = date(2026, 4, 23)
    mgr.write_today("only today", today)
    mgr.rotate_for_new_day(today)
    mgr.rotate_for_new_day(today)
    assert mgr.path_for_date(today).read_text() == "only today"
