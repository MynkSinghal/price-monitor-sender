"""Thread-safe in-memory state for job completions + JSON persistence.

Recorded per job:
    * st_ctime (Windows true creation time — Q14)
    * first_seen_wall_clock (UTC epoch) for audit
    * source_path (absolute path of the success.txt file)

Persistence model:
    * Every successful record() call writes atomically to state.json.
    * Read-heavy snapshot() operations work entirely off the in-memory dict.
    * Daily reset clears both memory and the JSON file (Q8).

Locking model:
    * One RLock protects writes. Reads return a shallow copy of the current
      dict, so the main thread never sees a torn view.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from .logger_setup import get_logger

log = get_logger("state")


@dataclass(frozen=True)
class JobRecord:
    job_name: str
    st_ctime_epoch: float       # Windows st_ctime (creation timestamp)
    first_seen_epoch: float     # UTC epoch of first detection on this sender
    source_path: str


class StateManager:
    """First-detection-locked store (Q18)."""

    def __init__(
        self,
        state_file: Path,
        first_detection_locked: bool = True,
        *,
        business_tz: str = "Europe/London",
        stale_cutoff_provider: Callable[[], float] | None = None,
    ) -> None:
        """Args:
            stale_cutoff_provider: optional callable returning the epoch
                below which any new st_ctime is considered "stale" (from a
                previous business day) and silently ignored. Used to
                prevent leakage of pre-cutoff success.txt files when daily
                reset failed to clear them.
        """
        self._state_file = state_file
        self._lock = threading.RLock()
        self._records: dict[str, JobRecord] = {}
        self._first_lock = first_detection_locked
        self._business_tz = ZoneInfo(business_tz)
        self._stale_cutoff_provider = stale_cutoff_provider
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    def _format_business_dt(self, epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=self._business_tz).strftime("%d/%m/%Y %H:%M:%S")

    def set_stale_cutoff_provider(self, provider: Callable[[], float] | None) -> None:
        with self._lock:
            self._stale_cutoff_provider = provider

    # ------------------------- persistence -------------------------

    def _load_from_disk(self) -> None:
        if not self._state_file.exists():
            log.info("No existing state file at %s — starting empty", self._state_file)
            return
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Corrupt state.json (%s) — starting empty", exc)
            return
        for job_name, payload in raw.get("records", {}).items():
            try:
                self._records[job_name] = JobRecord(
                    job_name=job_name,
                    st_ctime_epoch=float(payload["st_ctime_epoch"]),
                    first_seen_epoch=float(payload["first_seen_epoch"]),
                    source_path=str(payload["source_path"]),
                )
            except (KeyError, TypeError, ValueError):
                log.warning("Skipping malformed state entry for %s", job_name)
        log.info("Loaded %d records from %s", len(self._records), self._state_file)

    def _persist(self) -> None:
        payload = {
            "version": 1,
            "saved_at_epoch": time.time(),
            "records": {name: asdict(rec) for name, rec in self._records.items()},
        }
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".state-", suffix=".json.tmp", dir=str(self._state_file.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._state_file)
        except OSError as exc:
            log.error("Failed to persist state.json: %s", exc, exc_info=True)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------- public API -------------------------

    def record(self, job_name: str, st_ctime_epoch: float, source_path: str) -> bool:
        """Register a job completion. Returns True if stored, False if skipped.

        First-detection-locked semantics (Q18):
            * If a record already exists and first_detection_locked is True,
              the new timestamp is IGNORED (first detection wins for the day).
            * If first_detection_locked is False, the newer st_ctime wins
              (kept as an opt-in via config for future requirements).
        """
        with self._lock:
            # Stale-file guard: ignore success.txt whose creation time falls
            # before the current business-day cutoff. Protects against
            # leftover files from yesterday that weren't cleaned up at reset.
            if self._stale_cutoff_provider is not None:
                cutoff = self._stale_cutoff_provider()
                if st_ctime_epoch < cutoff:
                    stale_dt = self._format_business_dt(st_ctime_epoch)
                    cutoff_dt = self._format_business_dt(cutoff)
                    log.warning(
                        "STALE %s | st_ctime=%s < business_day_cutoff=%s | ignoring (left over from previous day) | path=%s",
                        job_name, stale_dt, cutoff_dt, source_path,
                    )
                    return False

            existing = self._records.get(job_name)
            if existing is not None and self._first_lock:
                locked_dt = self._format_business_dt(existing.st_ctime_epoch)
                log.info("IGNORED %s (already recorded at %s)", job_name, locked_dt)
                return False
            if existing is not None and st_ctime_epoch <= existing.st_ctime_epoch:
                return False
            self._records[job_name] = JobRecord(
                job_name=job_name,
                st_ctime_epoch=st_ctime_epoch,
                first_seen_epoch=time.time(),
                source_path=source_path,
            )
            self._persist()
            completed_dt = self._format_business_dt(st_ctime_epoch)
            log.info("RECORDED %s | completed=%s | path=%s", job_name, completed_dt, source_path)
            return True

    def get(self, job_name: str) -> JobRecord | None:
        with self._lock:
            return self._records.get(job_name)

    def snapshot(self) -> dict[str, JobRecord]:
        with self._lock:
            return dict(self._records)

    def completion_ts_for(
        self, jobs: Iterable[str], *, match_mode: str = "all"
    ) -> float | None:
        """Return the price-group timestamp given a job list and a match policy.

        match_mode="all" (default — composite AND, used by every existing row):
            MAX(st_ctime) across all jobs, only when EVERY job has a record.
            None if any job is missing.

        match_mode="any" (OR, used by JSE/JSE1-style rows where either or both
        files may arrive on a given day, and the row should flag in all cases):
            MAX(st_ctime) over the jobs that DO have records.
            None only when no listed job has been seen yet.
            Practical effect:
              - only one job present  -> that job's st_ctime ("flag on first arrival")
              - both jobs present     -> MAX of the two (latest wins)

        Empty jobs list always returns None — used by always-empty audit rows
        (manual-fill prices, KSE clients, etc. that we still want in the CSV
        grid with a blank timestamp).
        """
        jobs_list = [j for j in jobs]
        if not jobs_list:
            return None
        with self._lock:
            latest = -1.0
            seen_any = False
            for j in jobs_list:
                rec = self._records.get(j)
                if rec is None:
                    if match_mode == "all":
                        return None
                    continue
                seen_any = True
                if rec.st_ctime_epoch > latest:
                    latest = rec.st_ctime_epoch
            if not seen_any:
                return None
            return latest

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            try:
                if self._state_file.exists():
                    self._state_file.unlink()
            except OSError as exc:
                log.error("Failed to delete state file: %s", exc)
            log.info("State cleared (daily reset)")
