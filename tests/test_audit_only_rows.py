"""Audit-only rows (jobs=[], active=true) must appear in every CSV cycle
with an empty timestamp, never causing watcher attaches or completion timestamps.

These rows exist for KSE client prices, manual-fill prices (RCFT, PCFF, PDCE),
and rows whose schedule doesn't have a corresponding RANTask job
(PSTM/SSTM/POMT/SOMT, ISTM).
"""

from __future__ import annotations

from pathlib import Path

from src.csv_builder import CsvBuilder
from src.config_loader import AppConfig, PriceGroupDef
from src.state_manager import StateManager


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(
        prices_root=tmp_path,
        config_dir=tmp_path,
        state_dir=tmp_path,
        backup_dir=tmp_path,
        log_dir=tmp_path,
        receiver_url="http://x/y",
        receiver_timeout_seconds=15,
        http_method="POST",
        content_type="text/csv",
        extra_headers={},
        sender_hostname="testhost",
        sender_env="TEST",
        sender_site="UKPROD",
        send_mode="every_minute",
        send_interval_seconds=60,
        active_weekdays=(0, 1, 2, 3, 4),
        timezone_business="Europe/London",
        business_day_start="06:00",
        business_day_anchor="start",
        business_day_window={},
        business_night_window={},
        heartbeat_max_silence_seconds=300,
        log_level="INFO",
        log_filename_pattern="sender-%Y-%m-%d.log",
        log_max_bytes_per_file=1_000_000,
        log_backup_count_within_day=5,
        csv_delimiter="|",
        csv_header=("price_group_name", "timestamp"),
        csv_timestamp_format="%d/%m/%Y %H:%M:%S",
        csv_emit_header=False,
        csv_empty_timestamp_token="",
        backup_filename_template="pricecapture_backup_{date}.csv",
        backup_date_format="%Y-%m-%d",
        backup_retention_days=7,
        success_filename="success.txt",
        job_subfolder="GetPricesResult",
        first_detection_locked=True,
    )


def test_audit_only_row_emits_empty_timestamp(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    state = StateManager(tmp_path / "state.json")
    state.record("REAL_JOB", 1_700_000_000.0, "/x")
    builder = CsvBuilder(cfg, state)

    groups = (
        PriceGroupDef("RCFT", jobs=()),                                     # audit only
        PriceGroupDef("Citadel KSE", jobs=()),                              # audit only
        PriceGroupDef("REAL_GROUP", jobs=("REAL_JOB",)),                    # normal
    )
    snap = builder.build(groups)

    lines = [ln for ln in snap.payload.splitlines() if ln]
    assert lines[0] == "RCFT|"
    assert lines[1] == "Citadel KSE|"
    assert lines[2].startswith("REAL_GROUP|")
    assert snap.complete_count == 1
    assert snap.pending_count == 2


def test_audit_only_row_with_custom_empty_token(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.csv_empty_timestamp_token = "pending"
    state = StateManager(tmp_path / "state.json")
    builder = CsvBuilder(cfg, state)

    snap = builder.build((PriceGroupDef("PDCE", jobs=()),))
    assert snap.payload.strip() == "PDCE|pending"
