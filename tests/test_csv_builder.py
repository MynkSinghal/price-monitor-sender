"""Grid-mode CSV builder tests (two-site UKPROD + USPROD layout).

The wire format now contains ONE row per active price group every cycle.
Rows that are not yet complete on this host carry the configured
empty_timestamp_token (default: blank) instead of a timestamp.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.config_loader import PriceGroupDef
from src.csv_builder import CsvBuilder
from src.state_manager import StateManager

_UK = ZoneInfo("Europe/London")


def _uk_ts(year: int, month: int, day: int, hh: int, mm: int = 0, ss: int = 0) -> float:
    """A UK-clock wall-time → epoch. Tests assert the CSV renders the same
    HH:MM:SS we put in, regardless of the host system timezone."""
    return datetime(year, month, day, hh, mm, ss, tzinfo=_UK).timestamp()


def _make_cfg(empty_token: str = ""):
    return SimpleNamespace(
        csv_delimiter="|",
        csv_header=("price_group_name", "timestamp"),
        csv_timestamp_format="%d/%m/%Y %H:%M:%S",
        csv_emit_header=False,
        csv_empty_timestamp_token=empty_token,
        sender_site="UKPROD",
        timezone_business="Europe/London",
    )


def test_complete_single_job_row_renders_timestamp(tmp_path: Path) -> None:
    state = StateManager(tmp_path / "state.json")
    state.record("NSE_IX_1", _uk_ts(2025, 7, 14, 14, 30, 42), "/x")

    builder = CsvBuilder(_make_cfg(), state)
    snap = builder.build((PriceGroupDef("NSE_IX_1", ("NSE_IX_1",)),))

    assert snap.complete_count == 1
    assert snap.pending_count == 0
    assert snap.payload == "NSE_IX_1|14/07/2025 14:30:42\n"


def test_incomplete_row_uses_empty_token_blank(tmp_path: Path) -> None:
    state = StateManager(tmp_path / "state.json")
    builder = CsvBuilder(_make_cfg(empty_token=""), state)
    snap = builder.build((PriceGroupDef("CME_SPAN2A", ("CME_SPAN2A",)),))

    assert snap.complete_count == 0
    assert snap.pending_count == 1
    assert snap.payload == "CME_SPAN2A|\n"


def test_incomplete_row_uses_empty_token_pending(tmp_path: Path) -> None:
    state = StateManager(tmp_path / "state.json")
    builder = CsvBuilder(_make_cfg(empty_token="pending"), state)
    snap = builder.build((PriceGroupDef("CME_SPAN2A", ("CME_SPAN2A",)),))

    assert snap.payload == "CME_SPAN2A|pending\n"


def test_grid_mode_emits_every_active_group_in_order(tmp_path: Path) -> None:
    """Every active group is in the payload — completed AND not-yet-completed."""
    state = StateManager(tmp_path / "state.json")
    state.record("NSE_IX_1", _uk_ts(2025, 7, 14, 14, 30, 42), "/x")
    builder = CsvBuilder(_make_cfg(), state)

    groups = (
        PriceGroupDef("NSE_IX_1", ("NSE_IX_1",)),
        PriceGroupDef("CME_SPAN2A", ("CME_SPAN2A",)),
        PriceGroupDef("OCC_CPM", ("OCC_CPM",)),
    )
    snap = builder.build(groups)

    assert snap.complete_count == 1
    assert snap.pending_count == 2
    assert snap.payload == (
        "NSE_IX_1|14/07/2025 14:30:42\n"
        "CME_SPAN2A|\n"
        "OCC_CPM|\n"
    )
    assert [r.price_group_name for r in snap.rows] == ["NSE_IX_1", "CME_SPAN2A", "OCC_CPM"]


def test_composite_uses_max_timestamp_when_complete(tmp_path: Path) -> None:
    state = StateManager(tmp_path / "state.json")
    state.record("ICE_GSPD",   _uk_ts(2025, 7, 14, 11, 0, 12), "/x")
    state.record("ICE_GPDR",   _uk_ts(2025, 7, 14, 11, 5, 30), "/x")
    state.record("ICEUS_GSPD", _uk_ts(2025, 7, 14, 11, 3, 59), "/x")

    builder = CsvBuilder(_make_cfg(), state)
    snap = builder.build((PriceGroupDef(
        "ICE_GSPD / ICE_GPDR / ICEUS_GSPD",
        ("ICE_GSPD", "ICE_GPDR", "ICEUS_GSPD"),
    ),))

    assert snap.payload == "ICE_GSPD / ICE_GPDR / ICEUS_GSPD|14/07/2025 11:05:30\n"


def test_partial_composite_emits_empty_token_not_omitted(tmp_path: Path) -> None:
    """Old behaviour omitted partial composites entirely. New: row appears with empty timestamp."""
    state = StateManager(tmp_path / "state.json")
    state.record("ICE_GSPD", _uk_ts(2025, 7, 14, 11, 0), "/x")

    builder = CsvBuilder(_make_cfg(), state)
    snap = builder.build((PriceGroupDef(
        "ICE_GSPD / ICE_GPDR / ICEUS_GSPD",
        ("ICE_GSPD", "ICE_GPDR", "ICEUS_GSPD"),
    ),))

    assert snap.complete_count == 0
    assert snap.pending_count == 1
    assert snap.payload == "ICE_GSPD / ICE_GPDR / ICEUS_GSPD|\n"


def test_shared_job_flows_into_both_groups(tmp_path: Path) -> None:
    """ATHISIN feeds both PATH (3-job composite) and IATH (standalone)."""
    state = StateManager(tmp_path / "state.json")
    state.record("ATHFIX", _uk_ts(2025, 7, 14, 17, 0, 10), "/x")
    state.record("ATHVCT", _uk_ts(2025, 7, 14, 17, 2, 20), "/x")
    state.record("ATHISIN", _uk_ts(2025, 7, 14, 17, 5, 33), "/x")

    builder = CsvBuilder(_make_cfg(), state)
    snap = builder.build((
        PriceGroupDef("PATH", ("ATHFIX", "ATHVCT", "ATHISIN")),
        PriceGroupDef("IATH", ("ATHISIN",)),
    ))

    assert snap.complete_count == 2
    assert snap.payload == (
        "PATH|14/07/2025 17:05:33\n"
        "IATH|14/07/2025 17:05:33\n"
    )


def test_path_row_present_with_empty_timestamp_when_pending(tmp_path: Path) -> None:
    state = StateManager(tmp_path / "state.json")
    state.record("ATHFIX", _uk_ts(2025, 7, 14, 17, 0, 0), "/x")
    state.record("ATHISIN", _uk_ts(2025, 7, 14, 17, 5, 7), "/x")

    builder = CsvBuilder(_make_cfg(), state)
    snap = builder.build((
        PriceGroupDef("PATH", ("ATHFIX", "ATHVCT", "ATHISIN")),
        PriceGroupDef("IATH", ("ATHISIN",)),
    ))

    assert snap.complete_count == 1
    assert snap.pending_count == 1
    # Both rows MUST appear; PATH carries empty timestamp because ATHVCT missing.
    assert snap.payload == (
        "PATH|\n"
        "IATH|14/07/2025 17:05:07\n"
    )


def test_empty_inputs_produce_empty_payload(tmp_path: Path) -> None:
    state = StateManager(tmp_path / "state.json")
    builder = CsvBuilder(_make_cfg(), state)
    snap = builder.build(())
    assert snap.payload == ""
    assert snap.complete_count == 0
    assert snap.pending_count == 0
    assert snap.rows == ()
