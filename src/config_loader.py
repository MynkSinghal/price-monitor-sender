"""Configuration loader.

Merges three sources in ascending priority:
    1. config/config.json           (non-secret runtime config, committed)
    2. config/price_groups.json     (static price group definitions, committed)
    3. .env                         (secrets / per-deployment overrides)

All paths are resolved against the project root (the parent of this package).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class PriceGroupDef:
    """Static definition of a price group — loaded once at startup (Q5)."""

    price_group_name: str
    jobs: tuple[str, ...]
    notes: str = ""
    match_mode: str = "all"  # "all" (composite AND, default) | "any" (OR, e.g. JSE/JSE1)

    @property
    def is_composite(self) -> bool:
        return len(self.jobs) > 1

    @property
    def is_audit_only(self) -> bool:
        """True when the row is in CSV but has no jobs to monitor.

        Used for manual-fill prices (RCFT, PCFF, PDCE), KSE client rows
        (Citadel/HSBC/Macquarie/STONEX/Marex/JPM/Jump), and other rows
        where we don't have a RANTask job. They appear every cycle with
        an empty timestamp.
        """
        return len(self.jobs) == 0


@dataclass
class AppConfig:
    """Aggregated, validated runtime configuration."""

    # ---- paths ----
    prices_root: Path
    config_dir: Path
    state_dir: Path
    backup_dir: Path
    log_dir: Path

    # ---- transport ----
    receiver_url: str
    receiver_timeout_seconds: int
    http_method: str
    content_type: str
    extra_headers: dict[str, str]

    # ---- sender identity ----
    sender_hostname: str
    sender_env: str
    sender_site: str  # "UKPROD" or "USPROD" — set in .env

    # ---- scheduling ----
    send_mode: str  # "every_minute" | "business_schedule"
    send_interval_seconds: int
    active_weekdays: tuple[int, ...]
    # Business-day timing (everything is anchored to this; see docs)
    # All "what date does this instant belong to?" decisions use these values.
    timezone_business: str            # IANA zone, e.g. "Europe/London". DST/BST aware.
    business_day_start: str           # "HH:MM" in timezone_business — also the daily-reset cutoff
    business_day_anchor: str          # "start" or "end" — which calendar date stamps the business day
    business_day_window: dict[str, Any]
    business_night_window: dict[str, Any]

    # ---- watchdog ----
    heartbeat_max_silence_seconds: int

    # ---- logging ----
    log_level: str
    log_filename_pattern: str
    log_max_bytes_per_file: int
    log_backup_count_within_day: int

    # ---- csv ----
    csv_delimiter: str
    csv_header: tuple[str, ...]
    csv_timestamp_format: str
    csv_emit_header: bool
    csv_empty_timestamp_token: str  # written for incomplete rows in grid-mode CSV

    # ---- backup ----
    backup_filename_template: str  # e.g. "pricecapture_backup_{date}.csv"
    backup_date_format: str        # e.g. "%Y-%m-%d"
    backup_retention_days: int     # how many past dates to keep (today + N-1 previous)

    # ---- monitoring ----
    success_filename: str
    job_subfolder: str
    first_detection_locked: bool

    # ---- sites (two-site UKPROD + USPROD layout) ----
    usprod_jobs: tuple[str, ...] = ()

    # ---- data ----
    price_groups: tuple[PriceGroupDef, ...] = field(default_factory=tuple)

    # ----- helpers for the two-site layout -----

    @property
    def is_usprod(self) -> bool:
        return self.sender_site == "USPROD"

    @property
    def is_ukprod(self) -> bool:
        return self.sender_site == "UKPROD"

    @property
    def cross_site_jobs(self) -> frozenset[str]:
        """Jobs that physically live on the OTHER host.

        On UKPROD: jobs in usprod_jobs (we'll never see their success.txt).
        On USPROD: every job NOT in usprod_jobs (mirror).
        """
        usp = frozenset(self.usprod_jobs)
        if self.is_usprod:
            local_jobs = {j for pg in self.active_price_groups for j in pg.jobs}
            return frozenset(local_jobs - usp)
        return usp

    @property
    def active_price_groups(self) -> tuple[PriceGroupDef, ...]:
        """All price groups with active=true.

        Includes audit-only rows (jobs=[]) so they appear in the CSV grid
        with an empty timestamp every cycle. Per Q-2026-05 clarification:
        manual-fill prices and KSE client rows must be visible to the
        receiver as 'present but pending' rather than dropped silently.
        """
        return self.price_groups

    def resolve_job_path(self, job_name: str) -> Path:
        return self.prices_root / job_name / self.job_subfolder / self.success_filename

    def resolve_job_watch_dir(self, job_name: str) -> Path:
        return self.prices_root / job_name / self.job_subfolder


def _env_path(env_val: str | None, default_rel: str) -> Path:
    if not env_val:
        return PROJECT_ROOT / default_rel
    p = Path(env_val)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _parse_weekdays(raw: str) -> tuple[int, ...]:
    out: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        v = int(part)
        if v < 0 or v > 6:
            raise ValueError(f"ACTIVE_WEEKDAYS value out of range: {v}")
        out.append(v)
    return tuple(sorted(set(out)))


def _build_price_groups(raw_groups: list[dict[str, Any]]) -> tuple[PriceGroupDef, ...]:
    """Return only price groups with active=True AND non-empty job list.

    Rows with active=False (OBS-A exclusions, needs_verification, etc.) are dropped
    but kept in the JSON for audit purposes.

    Note on Q6: the original Q6 answer was 'no job overlap', but operational
    reality (confirmed later) is that SOME jobs DO feed multiple price groups —
    for example ATHISIN feeds both the PATH composite (ATHFIX + ATHVCT + ATHISIN)
    and the IATH standalone group. When a shared job's success.txt appears,
    every price group that lists that job picks it up independently. The
    state manager is keyed by job, so one record serves all its groups.

    Overlaps are therefore ALLOWED. We still report them at startup so that
    reviewers can spot accidental overlaps vs intentional ones.
    """
    import logging
    logger = logging.getLogger("price_sender.config")

    defs: list[PriceGroupDef] = []
    job_to_groups: dict[str, list[str]] = {}
    audit_only_count = 0
    for row in raw_groups:
        if not row.get("active", False):
            continue
        name = row["price_group_name"].strip()
        jobs = tuple(j.strip() for j in row.get("jobs", []) if j and j.strip())
        match_mode = str(row.get("match_mode", "all")).strip().lower()
        if match_mode not in {"all", "any"}:
            raise ValueError(
                f"price_group {name!r}: match_mode must be 'all' or 'any', got {match_mode!r}"
            )
        for job in jobs:
            job_to_groups.setdefault(job, []).append(name)
        if not jobs:
            audit_only_count += 1
        defs.append(PriceGroupDef(
            price_group_name=name,
            jobs=jobs,
            notes=row.get("notes", ""),
            match_mode=match_mode,
        ))

    if audit_only_count:
        logger.info(
            "Loaded %d audit-only price group(s) with no jobs (always emitted "
            "with empty timestamp — manual-fill prices, KSE clients, etc.)",
            audit_only_count,
        )

    overlaps = {job: groups for job, groups in job_to_groups.items() if len(groups) > 1}
    if overlaps:
        sample = "; ".join(
            f"{job} -> [{', '.join(groups)}]" for job, groups in list(overlaps.items())[:5]
        )
        logger.info(
            "Detected %d jobs feeding multiple price groups (expected for composites "
            "that share auxiliary jobs). Examples: %s",
            len(overlaps), sample,
        )
    return tuple(defs)


def load_config() -> AppConfig:
    load_dotenv(PROJECT_ROOT / ".env")

    config_dir = _env_path(os.getenv("CONFIG_DIR"), "config")
    state_dir = _env_path(os.getenv("STATE_DIR"), "state")
    backup_dir = _env_path(os.getenv("BACKUP_DIR"), "backups")
    log_dir = _env_path(os.getenv("LOG_DIR"), "logs")

    for p in (state_dir, backup_dir, log_dir):
        p.mkdir(parents=True, exist_ok=True)

    runtime = _load_json(config_dir / "config.json")
    groups_raw = _load_json(config_dir / "price_groups.json")["price_groups"]

    cfg = AppConfig(
        prices_root=Path(os.getenv("PRICES_ROOT", r"C:\Prices")),
        config_dir=config_dir,
        state_dir=state_dir,
        backup_dir=backup_dir,
        log_dir=log_dir,
        receiver_url=os.environ["RECEIVER_URL"],
        receiver_timeout_seconds=int(os.getenv("RECEIVER_TIMEOUT_SECONDS", "15")),
        http_method=runtime["transport"]["method"].upper(),
        content_type=runtime["transport"]["content_type"],
        extra_headers=dict(runtime["transport"].get("extra_headers", {})),
        sender_hostname=os.getenv("SENDER_HOSTNAME", "UNKNOWN"),
        sender_env=os.getenv("SENDER_ENV", "DEV"),
        sender_site=os.getenv("SENDER_SITE", "").strip().upper(),
        send_mode=os.getenv("SEND_MODE", "every_minute").strip().lower(),
        send_interval_seconds=int(os.getenv("SEND_INTERVAL_SECONDS", "60")),
        active_weekdays=_parse_weekdays(os.getenv("ACTIVE_WEEKDAYS", "0,1,2,3,4")),
        timezone_business=os.getenv(
            "BUSINESS_TIMEZONE",
            runtime["scheduler"].get("timezone_business", "Europe/London"),
        ),
        business_day_start=os.getenv(
            "BUSINESS_DAY_START",
            runtime["scheduler"].get("business_day_start", "06:00"),
        ),
        business_day_anchor=os.getenv(
            "BUSINESS_DAY_ANCHOR",
            runtime["scheduler"].get("business_day_anchor", "start"),
        ).strip().lower(),
        business_day_window=runtime["scheduler"]["business_schedule"]["day_window"],
        business_night_window=runtime["scheduler"]["business_schedule"]["night_window"],
        heartbeat_max_silence_seconds=int(os.getenv("HEARTBEAT_MAX_SILENCE_SECONDS", "300")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_filename_pattern=runtime["logging"]["filename_pattern"],
        log_max_bytes_per_file=int(runtime["logging"]["max_bytes_per_file"]),
        log_backup_count_within_day=int(runtime["logging"]["backup_count_within_day"]),
        csv_delimiter=runtime["csv"]["delimiter"],
        csv_header=tuple(runtime["csv"]["header"]),
        csv_timestamp_format=runtime["csv"]["timestamp_format"],
        csv_emit_header=bool(runtime["csv"]["emit_header"]),
        csv_empty_timestamp_token=str(runtime["csv"].get("empty_timestamp_token", "")),
        backup_filename_template=runtime["backup"]["filename_template"],
        backup_date_format=runtime["backup"]["date_format"],
        backup_retention_days=int(runtime["backup"].get("retention_days", 2)),
        success_filename=runtime["monitoring"]["success_filename"],
        job_subfolder=runtime["monitoring"]["job_subfolder"],
        first_detection_locked=bool(runtime["monitoring"]["first_detection_locked"]),
        usprod_jobs=tuple(
            j.strip()
            for j in runtime.get("sites", {}).get("usprod_jobs", [])
            if isinstance(j, str) and j.strip()
        ),
        price_groups=_build_price_groups(groups_raw),
    )

    if cfg.send_mode not in {"every_minute", "business_schedule"}:
        raise ValueError(
            f"SEND_MODE must be 'every_minute' or 'business_schedule', got {cfg.send_mode!r}"
        )
    if ":" not in cfg.business_day_start:
        raise ValueError("BUSINESS_DAY_START must be HH:MM")
    if cfg.business_day_anchor not in {"start", "end"}:
        raise ValueError(
            f"BUSINESS_DAY_ANCHOR must be 'start' or 'end' (got {cfg.business_day_anchor!r})"
        )
    if cfg.sender_site not in {"UKPROD", "USPROD"}:
        raise ValueError(
            f"SENDER_SITE must be 'UKPROD' or 'USPROD' (got {cfg.sender_site!r}). "
            "Set it in .env — see .env.example."
        )
    return cfg
