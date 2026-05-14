"""Gap-filling tests for StateManager.

Covers:
- stale_cutoff_provider path (STALE log, returns False)
- corrupt / truncated state.json
- malformed per-job entries skipped
- _persist OSError: error logged, tmp cleaned
- clear() when unlink raises
- set_stale_cutoff_provider
- equal st_ctime on unlocked → silent False
- first-lock=False + equal timestamp → silent False
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.state_manager import StateManager, JobRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sm(tmp_path: Path, first_lock=True, cutoff: float | None = None) -> StateManager:
    state_file = tmp_path / "state" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    provider = (lambda: cutoff) if cutoff is not None else None
    return StateManager(
        state_file=state_file,
        first_detection_locked=first_lock,
        business_tz="Europe/London",
        stale_cutoff_provider=provider,
    )


EPOCH_NOW = time.time()


# ---------------------------------------------------------------------------
# Stale cutoff
# ---------------------------------------------------------------------------

class TestStaleCutoff:
    def test_stale_file_rejected(self, tmp_path):
        cutoff = EPOCH_NOW  # anything older is stale
        sm = _sm(tmp_path, cutoff=cutoff)
        result = sm.record("JOB1", EPOCH_NOW - 3600, "/path/success.txt")
        assert result is False

    def test_fresh_file_accepted(self, tmp_path):
        cutoff = EPOCH_NOW - 7200  # 2h ago: file is fresh
        sm = _sm(tmp_path, cutoff=cutoff)
        result = sm.record("JOB1", EPOCH_NOW, "/path/success.txt")
        assert result is True

    def test_file_exactly_at_cutoff_rejected(self, tmp_path):
        """st_ctime < cutoff (strict less-than). Exactly equal → accepted."""
        cutoff = EPOCH_NOW
        sm = _sm(tmp_path, cutoff=cutoff)
        # Exactly at cutoff → NOT stale (condition is `<`, not `<=`)
        result = sm.record("JOB1", EPOCH_NOW, "/path/success.txt")
        assert result is True

    def test_no_cutoff_provider_accepts_all(self, tmp_path):
        sm = _sm(tmp_path, cutoff=None)
        result = sm.record("JOB1", EPOCH_NOW - 86400, "/path/success.txt")
        assert result is True

    def test_set_stale_cutoff_provider_takes_effect(self, tmp_path):
        sm = _sm(tmp_path, cutoff=None)
        sm.set_stale_cutoff_provider(lambda: EPOCH_NOW + 9999)
        result = sm.record("JOB1", EPOCH_NOW, "/path/success.txt")
        assert result is False

    def test_stale_record_not_persisted(self, tmp_path):
        cutoff = EPOCH_NOW
        sm = _sm(tmp_path, cutoff=cutoff)
        sm.record("JOB1", EPOCH_NOW - 3600, "/path/success.txt")
        sm2 = _sm(tmp_path, cutoff=None)
        assert sm2.get("JOB1") is None


# ---------------------------------------------------------------------------
# Corrupt / malformed state.json
# ---------------------------------------------------------------------------

class TestCorruptStateFile:
    def test_empty_file_starts_empty(self, tmp_path):
        state_file = tmp_path / "state" / "state.json"
        state_file.parent.mkdir()
        state_file.write_text("")
        sm = StateManager(state_file=state_file, first_detection_locked=True,
                          business_tz="Europe/London")
        assert sm.snapshot() == {}

    def test_invalid_json_starts_empty(self, tmp_path):
        state_file = tmp_path / "state" / "state.json"
        state_file.parent.mkdir()
        state_file.write_text("{not valid json}")
        sm = StateManager(state_file=state_file, first_detection_locked=True,
                          business_tz="Europe/London")
        assert sm.snapshot() == {}

    def test_truncated_json_starts_empty(self, tmp_path):
        state_file = tmp_path / "state" / "state.json"
        state_file.parent.mkdir()
        state_file.write_text('{"version":1, "records": {')
        sm = StateManager(state_file=state_file, first_detection_locked=True,
                          business_tz="Europe/London")
        assert sm.snapshot() == {}

    def test_valid_records_mixed_with_malformed(self, tmp_path):
        state_file = tmp_path / "state" / "state.json"
        state_file.parent.mkdir()
        state_file.write_text(json.dumps({
            "version": 1,
            "records": {
                "GOOD_JOB": {
                    "job_name": "GOOD_JOB",
                    "st_ctime_epoch": EPOCH_NOW,
                    "first_seen_epoch": EPOCH_NOW,
                    "source_path": "/p",
                },
                "BAD_JOB": {"broken": True},  # missing required fields
            }
        }))
        sm = StateManager(state_file=state_file, first_detection_locked=True,
                          business_tz="Europe/London")
        assert sm.get("GOOD_JOB") is not None
        assert sm.get("BAD_JOB") is None

    def test_missing_state_file_starts_empty(self, tmp_path):
        state_file = tmp_path / "state" / "state.json"
        state_file.parent.mkdir()
        sm = StateManager(state_file=state_file, first_detection_locked=True,
                          business_tz="Europe/London")
        assert sm.snapshot() == {}

    def test_oserror_reading_state_starts_empty(self, tmp_path):
        state_file = tmp_path / "state" / "state.json"
        state_file.parent.mkdir()
        state_file.write_text("{}")
        with patch.object(Path, "read_text", side_effect=OSError("locked")):
            sm = StateManager(state_file=state_file, first_detection_locked=True,
                              business_tz="Europe/London")
        assert sm.snapshot() == {}


# ---------------------------------------------------------------------------
# _persist OSError
# ---------------------------------------------------------------------------

class TestPersistFailure:
    def test_persist_oserror_logs_error_not_raises(self, tmp_path, caplog):
        import logging
        sm = _sm(tmp_path)
        with patch("src.state_manager.os.replace", side_effect=OSError("disk full")):
            with caplog.at_level(logging.ERROR):
                sm.record("JOB1", EPOCH_NOW, "/p")
        # Must not raise

    def test_persist_oserror_cleans_up_tmp(self, tmp_path):
        sm = _sm(tmp_path)
        created_tmp = []
        real_mkstemp = tempfile.mkstemp

        def track_mkstemp(**kwargs):
            fd, path = real_mkstemp(**kwargs)
            created_tmp.append(path)
            return fd, path

        with patch("src.state_manager.tempfile.mkstemp", side_effect=track_mkstemp):
            with patch("src.state_manager.os.replace", side_effect=OSError("disk full")):
                sm.record("JOB1", EPOCH_NOW, "/p")

        for p in created_tmp:
            assert not Path(p).exists()


# ---------------------------------------------------------------------------
# clear() failures
# ---------------------------------------------------------------------------

class TestClearFailure:
    def test_clear_oserror_on_unlink_logs_error(self, tmp_path, caplog):
        import logging
        sm = _sm(tmp_path)
        sm.record("JOB1", EPOCH_NOW, "/p")
        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            with caplog.at_level(logging.ERROR):
                sm.clear()  # must not raise

    def test_clear_removes_in_memory_state(self, tmp_path):
        sm = _sm(tmp_path)
        sm.record("JOB1", EPOCH_NOW, "/p")
        sm.clear()
        assert sm.snapshot() == {}

    def test_clear_deletes_state_file_when_present(self, tmp_path):
        sm = _sm(tmp_path)
        sm.record("JOB1", EPOCH_NOW, "/p")
        state_file = sm._state_file
        assert state_file.exists()
        sm.clear()
        assert not state_file.exists()

    def test_clear_no_file_is_fine(self, tmp_path):
        sm = _sm(tmp_path)
        sm.clear()  # no state file yet, must not raise


# ---------------------------------------------------------------------------
# Edge cases: equal / unlocked timestamps
# ---------------------------------------------------------------------------

class TestRecordEdgeCases:
    def test_equal_ctime_unlocked_returns_false(self, tmp_path):
        """first_lock=False, same ctime as existing → silent False (not updated)."""
        sm = _sm(tmp_path, first_lock=False)
        sm.record("JOB1", 1000.0, "/p1")
        result = sm.record("JOB1", 1000.0, "/p2")
        assert result is False

    def test_newer_ctime_unlocked_updates(self, tmp_path):
        sm = _sm(tmp_path, first_lock=False)
        sm.record("JOB1", 1000.0, "/p1")
        result = sm.record("JOB1", 2000.0, "/p2")
        assert result is True
        assert sm.get("JOB1").st_ctime_epoch == 2000.0

    def test_older_ctime_unlocked_ignored(self, tmp_path):
        sm = _sm(tmp_path, first_lock=False)
        sm.record("JOB1", 2000.0, "/p1")
        result = sm.record("JOB1", 1000.0, "/p2")
        assert result is False
        assert sm.get("JOB1").st_ctime_epoch == 2000.0

    def test_first_lock_true_second_record_ignored(self, tmp_path):
        sm = _sm(tmp_path, first_lock=True)
        sm.record("JOB1", 1000.0, "/p1")
        result = sm.record("JOB1", 9999.0, "/p2")
        assert result is False
        assert sm.get("JOB1").st_ctime_epoch == 1000.0

    def test_record_new_job_always_stored(self, tmp_path):
        sm = _sm(tmp_path, first_lock=True)
        result = sm.record("BRAND_NEW_JOB", EPOCH_NOW, "/p")
        assert result is True

    def test_persistence_round_trip(self, tmp_path):
        sm = _sm(tmp_path)
        sm.record("JOB1", EPOCH_NOW, "/path/to/success.txt")
        sm2 = StateManager(
            state_file=sm._state_file,
            first_detection_locked=True,
            business_tz="Europe/London",
        )
        rec = sm2.get("JOB1")
        assert rec is not None
        assert rec.st_ctime_epoch == EPOCH_NOW
        assert rec.source_path == "/path/to/success.txt"
