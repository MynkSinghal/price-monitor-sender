"""Build the canonical CSV payload (grid mode, two-site aware).

Wire format (one row per *active* price group, in JSON order):
    price_group_name|<timestamp or empty token>

When this sender has all jobs for a price group:
    price_group_name|22/04/2026 14:30:42

When this sender has NOT yet completed all jobs for the group (cross-site
rows on the wrong host, or jobs that simply have not arrived today):
    price_group_name|<csv.empty_timestamp_token>      (default: blank)

Rules:
    * Single-job groups  → that job's st_ctime.
    * Composite groups   → MAX(st_ctime) across all jobs, only when ALL present.
    * Partial composites → row PRESENT but timestamp empty.
    * `active: false`    → row never on the wire (current behaviour kept).

The receiver merges UKPROD + USPROD payloads on its side: per row, whichever
side sent a non-empty timestamp wins (or take MAX if both are non-empty,
which only happens for jobs that genuinely exist on both sides).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config_loader import AppConfig, PriceGroupDef
from .logger_setup import get_logger
from .state_manager import StateManager

log = get_logger("csv")


@dataclass(frozen=True)
class CsvRow:
    price_group_name: str
    timestamp_epoch: float | None  # None when this host has not completed the group
    formatted: str  # dd/mm/yyyy hh:mm:ss OR the empty-timestamp token
    is_composite: bool
    completed_locally: bool


@dataclass(frozen=True)
class CsvSnapshot:
    rows: tuple[CsvRow, ...]
    payload: str
    generated_at_epoch: float
    complete_count: int  # rows with a real timestamp (completed locally)
    pending_count: int   # rows with empty token (not yet completed locally)


class CsvBuilder:
    def __init__(self, cfg: AppConfig, state: StateManager) -> None:
        self._cfg = cfg
        self._state = state
        self._business_tz = ZoneInfo(cfg.timezone_business)

    def _format_ts(self, epoch: float) -> str:
        # Render in business TZ so the CSV is always UK-time regardless of
        # whether this host's OS clock is in UK or US time.
        dt = datetime.fromtimestamp(epoch, tz=self._business_tz)
        return dt.strftime(self._cfg.csv_timestamp_format)

    def build(self, price_groups: tuple[PriceGroupDef, ...]) -> CsvSnapshot:
        rows: list[CsvRow] = []
        empty_token = self._cfg.csv_empty_timestamp_token
        complete = 0
        pending = 0
        for pg in price_groups:
            ts = self._state.completion_ts_for(pg.jobs, match_mode=pg.match_mode)
            if ts is None:
                rows.append(CsvRow(
                    price_group_name=pg.price_group_name,
                    timestamp_epoch=None,
                    formatted=empty_token,
                    is_composite=pg.is_composite,
                    completed_locally=False,
                ))
                pending += 1
            else:
                rows.append(CsvRow(
                    price_group_name=pg.price_group_name,
                    timestamp_epoch=ts,
                    formatted=self._format_ts(ts),
                    is_composite=pg.is_composite,
                    completed_locally=True,
                ))
                complete += 1

        buf = io.StringIO()
        delim = self._cfg.csv_delimiter
        if self._cfg.csv_emit_header:
            buf.write(delim.join(self._cfg.csv_header))
            buf.write("\n")
        for row in rows:
            buf.write(f"{row.price_group_name}{delim}{row.formatted}\n")

        snapshot = CsvSnapshot(
            rows=tuple(rows),
            payload=buf.getvalue(),
            generated_at_epoch=datetime.now().timestamp(),
            complete_count=complete,
            pending_count=pending,
        )
        log.debug(
            "CSV built | site=%s | rows=%d | complete=%d | pending=%d | bytes=%d",
            self._cfg.sender_site, len(rows), complete, pending, len(snapshot.payload),
        )
        return snapshot

    def write_today_backup(self, snapshot: CsvSnapshot, backup_dir: Path, filename: str) -> Path:
        backup_dir.mkdir(parents=True, exist_ok=True)
        out_path = backup_dir / filename
        out_path.write_text(snapshot.payload, encoding="utf-8")
        return out_path
