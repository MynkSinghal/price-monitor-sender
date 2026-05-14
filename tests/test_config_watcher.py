"""Tests for config_watcher.ConfigWatcher."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config_watcher import ConfigWatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _watcher(paths, interval=0.05) -> ConfigWatcher:
    return ConfigWatcher(paths, interval_seconds=interval)


# ---------------------------------------------------------------------------
# Construction / baseline
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_baseline_recorded_for_existing_file(self, tmp_path):
        f = tmp_path / "config.json"
        f.write_text("{}")
        w = _watcher([f])
        assert w._baseline[f.resolve()] == f.stat().st_mtime

    def test_baseline_zero_for_missing_file(self, tmp_path):
        missing = tmp_path / "missing.env"
        w = _watcher([missing])
        assert w._baseline[missing.resolve()] == 0.0

    def test_multiple_paths_all_baselined(self, tmp_path):
        f1 = tmp_path / "a.json"
        f1.write_text("a")
        f2 = tmp_path / "b.json"
        f2.write_text("b")
        w = _watcher([f1, f2])
        assert f1.resolve() in w._baseline
        assert f2.resolve() in w._baseline

    def test_paths_resolved_to_absolute(self, tmp_path):
        f = tmp_path / "x.json"
        f.write_text("{}")
        w = _watcher([f])
        for p in w._paths:
            assert p.is_absolute()


# ---------------------------------------------------------------------------
# _detect_changes
# ---------------------------------------------------------------------------

class TestDetectChanges:
    def test_no_change_returns_empty(self, tmp_path):
        f = tmp_path / "cfg.json"
        f.write_text("{}")
        w = _watcher([f])
        assert w._detect_changes() == []

    def test_modified_file_detected(self, tmp_path):
        f = tmp_path / "cfg.json"
        f.write_text("{}")
        w = _watcher([f])
        time.sleep(0.01)
        f.write_text('{"changed": true}')
        changed = w._detect_changes()
        assert f.resolve() in changed

    def test_deleted_file_detected_when_previously_existed(self, tmp_path):
        f = tmp_path / "cfg.json"
        f.write_text("{}")
        w = _watcher([f])
        f.unlink()
        changed = w._detect_changes()
        assert f.resolve() in changed

    def test_deleted_file_not_detected_when_never_existed(self, tmp_path):
        missing = tmp_path / "never.json"
        w = _watcher([missing])
        # baseline is 0.0; file still missing → no change
        changed = w._detect_changes()
        assert changed == []

    def test_newly_created_file_detected(self, tmp_path):
        """A file that didn't exist at construction (baseline=0.0) but now appears IS detected.

        This is intentional: a new config file appearing signals a restart is
        needed so the process picks it up via load_config().
        """
        new_file = tmp_path / "new.json"
        w = _watcher([new_file])
        # baseline is 0.0 (file was absent); now the file appears
        new_file.write_text("{}")
        changed = w._detect_changes()
        # 0.0 != real_mtime → detected as change
        assert new_file.resolve() in changed

    def test_multiple_files_only_changed_one_reported(self, tmp_path):
        f1 = tmp_path / "a.json"
        f2 = tmp_path / "b.json"
        f1.write_text("a")
        f2.write_text("b")
        w = _watcher([f1, f2])
        time.sleep(0.01)
        f1.write_text("a_modified")
        changed = w._detect_changes()
        assert f1.resolve() in changed
        assert f2.resolve() not in changed


# ---------------------------------------------------------------------------
# start / stop (thread behavior)
# ---------------------------------------------------------------------------

class TestStartStop:
    def test_stop_prevents_exit(self, tmp_path):
        """Stopping watcher before any change: os._exit should never be called."""
        f = tmp_path / "cfg.json"
        f.write_text("{}")
        w = _watcher([f], interval=0.02)
        w.start()
        time.sleep(0.05)  # let it poll once
        w.stop()
        # No file change → no exit. Just confirm thread exits cleanly.
        w._thread.join(timeout=0.5)

    def test_change_triggers_os_exit(self, tmp_path):
        """Modify a file after starting: _exit(0) must be called."""
        f = tmp_path / "cfg.json"
        f.write_text("{}")
        w = _watcher([f], interval=0.03)

        exit_called = threading.Event()

        def fake_exit(code):
            exit_called.set()

        with patch("src.config_watcher.os._exit", side_effect=fake_exit):
            w.start()
            time.sleep(0.01)
            f.write_text('{"changed": 1}')
            triggered = exit_called.wait(timeout=2.0)

        assert triggered, "os._exit(0) was not called after file change"

    def test_exit_called_with_code_0(self, tmp_path):
        f = tmp_path / "cfg.json"
        f.write_text("{}")
        w = _watcher([f], interval=0.03)

        exit_codes = []

        def fake_exit(code):
            exit_codes.append(code)
            raise SystemExit(code)

        with patch("src.config_watcher.os._exit", side_effect=fake_exit):
            w.start()
            time.sleep(0.01)
            f.write_text("changed")
            time.sleep(0.5)

        assert 0 in exit_codes

    def test_stop_while_no_change_is_clean(self, tmp_path):
        f = tmp_path / "cfg.json"
        f.write_text("{}")
        w = _watcher([f], interval=0.02)
        w.start()
        w.stop()
        w._thread.join(timeout=0.5)
        assert not w._thread.is_alive()

    def test_thread_is_daemon(self, tmp_path):
        f = tmp_path / "cfg.json"
        f.write_text("{}")
        w = _watcher([f])
        assert w._thread.daemon is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_paths_list(self):
        w = _watcher([])
        assert w._detect_changes() == []

    def test_watcher_does_not_fire_on_unrelated_file(self, tmp_path):
        watched = tmp_path / "cfg.json"
        watched.write_text("{}")
        unrelated = tmp_path / "other.txt"
        unrelated.write_text("hi")
        w = _watcher([watched])
        unrelated.write_text("changed")
        assert w._detect_changes() == []

    def test_two_simultaneous_changes_both_reported(self, tmp_path):
        f1 = tmp_path / "a.json"
        f2 = tmp_path / "b.json"
        f1.write_text("a")
        f2.write_text("b")
        w = _watcher([f1, f2])
        time.sleep(0.01)
        f1.write_text("a2")
        f2.write_text("b2")
        changed = w._detect_changes()
        assert f1.resolve() in changed
        assert f2.resolve() in changed
