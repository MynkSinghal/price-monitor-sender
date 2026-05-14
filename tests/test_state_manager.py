from __future__ import annotations

from pathlib import Path

from src.state_manager import StateManager


def test_first_detection_is_locked(tmp_path: Path) -> None:
    state = StateManager(tmp_path / "state.json", first_detection_locked=True)
    assert state.record("JOB_A", 1_000.0, "/x/JOB_A/success.txt") is True
    assert state.record("JOB_A", 2_000.0, "/x/JOB_A/success.txt") is False
    rec = state.get("JOB_A")
    assert rec is not None
    assert rec.st_ctime_epoch == 1_000.0


def test_latest_wins_when_not_locked(tmp_path: Path) -> None:
    state = StateManager(tmp_path / "state.json", first_detection_locked=False)
    assert state.record("JOB_B", 1_000.0, "/x") is True
    assert state.record("JOB_B", 500.0, "/x") is False
    assert state.record("JOB_B", 1_500.0, "/x") is True
    assert state.get("JOB_B").st_ctime_epoch == 1_500.0


def test_composite_returns_max_when_all_present(tmp_path: Path) -> None:
    state = StateManager(tmp_path / "state.json")
    state.record("ICE_GSPD", 1_000.0, "/x")
    state.record("ICE_GPDR", 3_000.0, "/x")
    state.record("ICEUS_GSPD", 2_000.0, "/x")
    ts = state.completion_ts_for(["ICE_GSPD", "ICE_GPDR", "ICEUS_GSPD"])
    assert ts == 3_000.0


def test_composite_returns_none_when_any_missing(tmp_path: Path) -> None:
    state = StateManager(tmp_path / "state.json")
    state.record("ICE_GSPD", 1_000.0, "/x")
    state.record("ICE_GPDR", 3_000.0, "/x")
    ts = state.completion_ts_for(["ICE_GSPD", "ICE_GPDR", "ICEUS_GSPD"])
    assert ts is None


def test_persistence_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    s1 = StateManager(path)
    s1.record("JOB_P", 42.5, "/x")
    s2 = StateManager(path)
    rec = s2.get("JOB_P")
    assert rec is not None
    assert rec.st_ctime_epoch == 42.5


def test_clear_wipes_state_and_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    s = StateManager(path)
    s.record("JOB_X", 1.0, "/x")
    assert path.exists()
    s.clear()
    assert s.get("JOB_X") is None
    assert not path.exists()
 