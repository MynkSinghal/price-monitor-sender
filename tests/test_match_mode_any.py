"""Tests for match_mode='any' (OR-semantic) price groups, e.g. JSE1/JSE.

Per Q-2026-05 clarification:
  - Some rows have multiple jobs where EITHER or BOTH may arrive on a given day.
  - The row should be flagged in all cases.
  - Timestamp policy: FIRST when only one arrives, MAX when both arrive.

This is implemented as `match_mode="any"` on the row and a corresponding
branch in `StateManager.completion_ts_for(jobs, match_mode=...)`.
"""

from __future__ import annotations

from pathlib import Path

from src.state_manager import StateManager


def test_any_mode_returns_none_when_no_job_seen(tmp_path: Path) -> None:
    state = StateManager(tmp_path / "state.json")
    assert state.completion_ts_for(["JSE", "JSE1"], match_mode="any") is None


def test_any_mode_uses_only_arrived_job_when_just_one_present(tmp_path: Path) -> None:
    """FIRST-wins behaviour when only a single job has arrived."""
    state = StateManager(tmp_path / "state.json")
    state.record("JSE", 1_500.0, "/x/JSE/success.txt")
    ts = state.completion_ts_for(["JSE", "JSE1"], match_mode="any")
    assert ts == 1_500.0


def test_any_mode_uses_only_arrived_job_when_other_one_arrives(tmp_path: Path) -> None:
    state = StateManager(tmp_path / "state.json")
    state.record("JSE1", 2_750.0, "/x/JSE1/success.txt")
    ts = state.completion_ts_for(["JSE", "JSE1"], match_mode="any")
    assert ts == 2_750.0


def test_any_mode_returns_max_when_both_arrive(tmp_path: Path) -> None:
    """MAX-wins behaviour when every listed job has arrived."""
    state = StateManager(tmp_path / "state.json")
    state.record("JSE", 1_500.0, "/x/JSE/success.txt")
    state.record("JSE1", 2_750.0, "/x/JSE1/success.txt")
    ts = state.completion_ts_for(["JSE", "JSE1"], match_mode="any")
    assert ts == 2_750.0


def test_all_mode_still_requires_every_job(tmp_path: Path) -> None:
    """Default 'all' semantics must remain unchanged for every other row."""
    state = StateManager(tmp_path / "state.json")
    state.record("JSE", 1_500.0, "/x")
    assert state.completion_ts_for(["JSE", "JSE1"], match_mode="all") is None
    state.record("JSE1", 2_750.0, "/x")
    assert state.completion_ts_for(["JSE", "JSE1"], match_mode="all") == 2_750.0


def test_empty_jobs_list_always_returns_none(tmp_path: Path) -> None:
    """Audit-only rows (jobs=[]) must always be 'pending' regardless of mode."""
    state = StateManager(tmp_path / "state.json")
    state.record("ANYTHING", 9_999.0, "/x")
    assert state.completion_ts_for([], match_mode="all") is None
    assert state.completion_ts_for([], match_mode="any") is None
