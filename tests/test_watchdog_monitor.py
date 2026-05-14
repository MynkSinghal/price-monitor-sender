"""Tests for watchdog_monitor.WatchdogMonitor and _SuccessFileHandler."""

from __future__ import annotations

import os
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from src.watchdog_monitor import WatchdogMonitor, _SuccessFileHandler
from src.state_manager import StateManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path, prices_root: Path | None = None) -> types.SimpleNamespace:
    pr = prices_root or (tmp_path / "prices")
    pr.mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(
        prices_root=pr,
        success_filename="success.txt",
        watch_subdir="GetPricesResult",
    )


def _make_state() -> MagicMock:
    state = MagicMock(spec=StateManager)
    state.record.return_value = True
    return state


def _job_dir(cfg, job: str) -> Path:
    return cfg.prices_root / job / cfg.watch_subdir


def _resolve_job_path(cfg, job: str) -> Path:
    return _job_dir(cfg, job) / cfg.success_filename


def _setup_job(cfg, job: str) -> Path:
    d = _job_dir(cfg, job)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_monitor(cfg, state) -> WatchdogMonitor:
    """Build a WatchdogMonitor with resolve_job_path/watch_dir wired to cfg."""
    cfg.resolve_job_path = lambda job: _resolve_job_path(cfg, job)
    cfg.resolve_job_watch_dir = lambda job: _job_dir(cfg, job)
    return WatchdogMonitor(cfg, state)


# ---------------------------------------------------------------------------
# _SuccessFileHandler._maybe_record
# ---------------------------------------------------------------------------

class TestSuccessFileHandler:
    def test_records_matching_filename(self, tmp_path):
        state = _make_state()
        handler = _SuccessFileHandler("JOB1", "success.txt", state)
        f = tmp_path / "success.txt"
        f.write_text("done")
        handler._maybe_record(str(f))
        state.record.assert_called_once()
        args = state.record.call_args[0]
        assert args[0] == "JOB1"

    def test_ignores_wrong_filename(self, tmp_path):
        state = _make_state()
        handler = _SuccessFileHandler("JOB1", "success.txt", state)
        f = tmp_path / "other.txt"
        f.write_text("data")
        handler._maybe_record(str(f))
        state.record.assert_not_called()

    def test_filename_match_is_case_insensitive(self, tmp_path):
        state = _make_state()
        handler = _SuccessFileHandler("JOB1", "SUCCESS.TXT", state)
        f = tmp_path / "success.txt"
        f.write_text("done")
        handler._maybe_record(str(f))
        state.record.assert_called_once()

    def test_warns_on_file_not_found(self, tmp_path, caplog):
        import logging
        state = _make_state()
        handler = _SuccessFileHandler("JOB1", "success.txt", state)
        missing = str(tmp_path / "success.txt")
        with caplog.at_level(logging.WARNING):
            handler._maybe_record(missing)
        state.record.assert_not_called()

    def test_logs_error_on_oserror(self, tmp_path, caplog):
        import logging
        state = _make_state()
        handler = _SuccessFileHandler("JOB1", "success.txt", state)
        f = tmp_path / "success.txt"
        f.write_text("x")
        with patch("src.watchdog_monitor.os.stat", side_effect=OSError("perm denied")):
            with caplog.at_level(logging.ERROR):
                handler._maybe_record(str(f))
        state.record.assert_not_called()

    def test_passes_st_ctime_to_state(self, tmp_path):
        state = _make_state()
        handler = _SuccessFileHandler("JOB1", "success.txt", state)
        f = tmp_path / "success.txt"
        f.write_text("done")
        ctime = f.stat().st_ctime
        handler._maybe_record(str(f))
        args = state.record.call_args[0]
        assert abs(args[1] - ctime) < 1.0


# ---------------------------------------------------------------------------
# WatchdogMonitor.reconcile_existing_files
# ---------------------------------------------------------------------------

class TestReconcileExistingFiles:
    def test_finds_existing_success_txt(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        monitor = _make_monitor(cfg, state)

        _setup_job(cfg, "JOB_A")
        success = _resolve_job_path(cfg, "JOB_A")
        success.write_text("done")

        found = monitor.reconcile_existing_files(["JOB_A"])
        assert found == 1
        state.record.assert_called_once()

    def test_missing_success_txt_not_counted(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        monitor = _make_monitor(cfg, state)
        _setup_job(cfg, "JOB_A")  # dir exists but no success.txt

        found = monitor.reconcile_existing_files(["JOB_A"])
        assert found == 0

    def test_missing_dir_skipped_gracefully(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        monitor = _make_monitor(cfg, state)

        found = monitor.reconcile_existing_files(["NO_DIR_JOB"])
        assert found == 0

    def test_multiple_jobs_all_reconciled(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        monitor = _make_monitor(cfg, state)

        for job in ["JOB_A", "JOB_B", "JOB_C"]:
            _setup_job(cfg, job)
            _resolve_job_path(cfg, job).write_text("done")

        found = monitor.reconcile_existing_files(["JOB_A", "JOB_B", "JOB_C"])
        assert found == 3

    def test_state_record_returns_false_not_counted(self, tmp_path):
        """If state.record returns False (already recorded), don't count."""
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        state.record.return_value = False
        monitor = _make_monitor(cfg, state)

        _setup_job(cfg, "JOB_A")
        _resolve_job_path(cfg, "JOB_A").write_text("done")

        found = monitor.reconcile_existing_files(["JOB_A"])
        assert found == 0

    def test_oserror_on_stat_skipped(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        monitor = _make_monitor(cfg, state)

        _setup_job(cfg, "JOB_A")
        success = _resolve_job_path(cfg, "JOB_A")
        success.write_text("done")

        with patch.object(Path, "stat", side_effect=OSError("perm")):
            found = monitor.reconcile_existing_files(["JOB_A"])
        assert found == 0

    def test_empty_jobs_list(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        monitor = _make_monitor(cfg, state)
        found = monitor.reconcile_existing_files([])
        assert found == 0


# ---------------------------------------------------------------------------
# WatchdogMonitor.start / stop
# ---------------------------------------------------------------------------

class TestStartStop:
    def test_start_idempotent(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        monitor = _make_monitor(cfg, state)
        _setup_job(cfg, "JOB_A")

        with patch.object(monitor._observer, "start"):
            with patch.object(monitor._observer, "schedule"):
                monitor.start(["JOB_A"])
                monitor.start(["JOB_A"])  # second call ignored
                assert monitor._observer.start.call_count == 1

    def test_stop_before_start_is_safe(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        monitor = _make_monitor(cfg, state)
        monitor.stop()  # must not raise

    def test_missing_dir_skipped_during_start(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        monitor = _make_monitor(cfg, state)

        with patch.object(monitor._observer, "start"):
            with patch.object(monitor._observer, "schedule") as mock_sched:
                monitor.start(["NO_DIR_JOB"])
                mock_sched.assert_not_called()


# ---------------------------------------------------------------------------
# WatchdogMonitor._attach_watches
# ---------------------------------------------------------------------------

class TestAttachWatches:
    def test_attaches_to_existing_dirs(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        monitor = _make_monitor(cfg, state)
        _setup_job(cfg, "JOB_A")

        with patch.object(monitor._observer, "schedule") as mock_sched:
            monitor._observer.start = MagicMock()
            monitor.start(["JOB_A"])
            assert mock_sched.call_count == 1

    def test_does_not_double_attach(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        monitor = _make_monitor(cfg, state)
        _setup_job(cfg, "JOB_A")

        with patch.object(monitor._observer, "schedule") as mock_sched:
            with patch.object(monitor._observer, "start"):
                monitor.start(["JOB_A"])
                n = monitor._attach_watches(["JOB_A"])
        assert n == 0  # already watched


# ---------------------------------------------------------------------------
# reconcile_and_watch
# ---------------------------------------------------------------------------

class TestReconcileAndWatch:
    def test_returns_tuple(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        state = _make_state()
        monitor = _make_monitor(cfg, state)

        with patch.object(monitor._observer, "start"):
            monitor.start([])
        result = monitor.reconcile_and_watch()
        assert isinstance(result, tuple)
        assert len(result) == 2
