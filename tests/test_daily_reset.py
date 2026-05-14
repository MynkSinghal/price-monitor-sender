"""Tests for daily_reset.perform_daily_reset."""

from __future__ import annotations

import types
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from src.daily_reset import perform_daily_reset


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path, log_filename_pattern: str = "sender-%Y-%m-%d.log") -> types.SimpleNamespace:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return types.SimpleNamespace(
        state_dir=state_dir,
        log_dir=log_dir,
        log_filename_pattern=log_filename_pattern,
        log_max_bytes_per_file=10_000_000,
        log_backup_count_within_day=3,
    )


def _make_state() -> MagicMock:
    return MagicMock()


def _make_backup() -> MagicMock:
    return MagicMock()


TODAY = date(2026, 5, 14)


# ---------------------------------------------------------------------------
# Basic flow
# ---------------------------------------------------------------------------

class TestPerformDailyResetBasicFlow:
    def test_calls_state_clear(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        backup = _make_backup()
        with patch("src.daily_reset.rotate_log_file"):
            perform_daily_reset(cfg, state, backup, TODAY)
        state.clear.assert_called_once()

    def test_calls_backup_rotate_for_new_day(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        backup = _make_backup()
        with patch("src.daily_reset.rotate_log_file"):
            perform_daily_reset(cfg, state, backup, TODAY)
        backup.rotate_for_new_day.assert_called_once_with(TODAY)

    def test_calls_rotate_log_file_first(self, tmp_path):
        """rotate_log_file must be called before state.clear (log rotates first)."""
        cfg = _make_cfg(tmp_path)
        call_order = []
        state = MagicMock()
        state.clear.side_effect = lambda: call_order.append("state.clear")
        backup = _make_backup()

        def fake_rotate(c, d):
            call_order.append("rotate_log_file")

        with patch("src.daily_reset.rotate_log_file", side_effect=fake_rotate):
            perform_daily_reset(cfg, state, backup, TODAY)

        assert call_order[0] == "rotate_log_file"
        assert "state.clear" in call_order

    def test_calls_purge_old_logs_with_correct_filenames(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        backup = _make_backup()
        with patch("src.daily_reset.rotate_log_file"):
            perform_daily_reset(cfg, state, backup, TODAY)

        expected_today = TODAY.strftime(cfg.log_filename_pattern)
        expected_yesterday = (TODAY - timedelta(days=1)).strftime(cfg.log_filename_pattern)
        backup.purge_old_logs.assert_called_once_with(
            Path(cfg.log_dir),
            keep_filenames={expected_today, expected_yesterday},
        )

    def test_idempotent_when_called_twice(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        backup = _make_backup()
        with patch("src.daily_reset.rotate_log_file"):
            perform_daily_reset(cfg, state, backup, TODAY)
            perform_daily_reset(cfg, state, backup, TODAY)
        assert state.clear.call_count == 2
        assert backup.rotate_for_new_day.call_count == 2


# ---------------------------------------------------------------------------
# Orphan .tmp cleanup
# ---------------------------------------------------------------------------

class TestOrphanTmpCleanup:
    def test_removes_orphan_tmp_files(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        orphan = cfg.state_dir / ".state-abc123.json.tmp"
        orphan.write_text("{}")

        state = _make_state()
        backup = _make_backup()
        with patch("src.daily_reset.rotate_log_file"):
            perform_daily_reset(cfg, state, backup, TODAY)

        assert not orphan.exists()

    def test_removes_multiple_orphans(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        orphans = [cfg.state_dir / f".state-{i}.json.tmp" for i in range(3)]
        for o in orphans:
            o.write_text("{}")

        state = _make_state()
        backup = _make_backup()
        with patch("src.daily_reset.rotate_log_file"):
            perform_daily_reset(cfg, state, backup, TODAY)

        assert all(not o.exists() for o in orphans)

    def test_non_tmp_files_left_alone(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        real_state = cfg.state_dir / "state.json"
        real_state.write_text('{"version":1}')

        state = _make_state()
        backup = _make_backup()
        with patch("src.daily_reset.rotate_log_file"):
            perform_daily_reset(cfg, state, backup, TODAY)

        assert real_state.exists()

    def test_oserror_on_orphan_unlink_is_logged_not_raised(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        orphan = cfg.state_dir / ".state-xyz.json.tmp"
        orphan.write_text("{}")

        state = _make_state()
        backup = _make_backup()
        with patch("src.daily_reset.rotate_log_file"):
            with patch.object(Path, "unlink", side_effect=OSError("locked")):
                # Should NOT raise
                perform_daily_reset(cfg, state, backup, TODAY)

    def test_no_orphans_is_fine(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        backup = _make_backup()
        with patch("src.daily_reset.rotate_log_file"):
            perform_daily_reset(cfg, state, backup, TODAY)  # must not raise


# ---------------------------------------------------------------------------
# Different dates
# ---------------------------------------------------------------------------

class TestDifferentDates:
    @pytest.mark.parametrize("d", [
        date(2026, 1, 1),    # New Year
        date(2026, 3, 29),   # DST change date (UK)
        date(2026, 12, 31),  # Year end
    ])
    def test_various_dates(self, tmp_path, d):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        backup = _make_backup()
        with patch("src.daily_reset.rotate_log_file"):
            perform_daily_reset(cfg, state, backup, d)
        backup.rotate_for_new_day.assert_called_once_with(d)
