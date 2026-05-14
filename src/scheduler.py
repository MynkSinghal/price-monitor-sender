"""Send-interval scheduler + business-day clock.

Concepts
========
The system runs in **business days** that span 06:00 → 06:00 in
``timezone_business`` (DST/BST aware via zoneinfo). The exact cutoff is
``business_day_start`` (HH:MM) in ``timezone_business`` and is the SINGLE
source of truth for:

    * What calendar date a price's ``st_ctime`` belongs to.
    * Which date stamps the CSV/backup filename and the in-memory state.
    * When the daily reset fires (it fires on business-day rollover).
    * Whether the weekday mask says "we are active today".

``business_day_anchor`` chooses how that ~24h window is mapped to a single
calendar date:

    * ``"start"`` (default) — the window starting at 06:00 on day D is
      labelled D. (i.e. 04:30 UK on the morning of D+1 is still business
      day D, because the next window has not opened yet.)
    * ``"end"`` — the same window is labelled D+1. Use when downstream
      teams refer to "today's prices" by the date the window closes.

Send modes (``SEND_MODE``)
=========================
* ``every_minute`` — fixed cadence of ``SEND_INTERVAL_SECONDS``.
* ``business_schedule`` — day/night windows defined in
  ``config/config.json`` → ``scheduler.business_schedule``. Interpreted
  in ``timezone_business``. DST-aware.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from .config_loader import AppConfig


@dataclass(frozen=True)
class ClockSnapshot:
    """A single instant rendered in every clock that matters."""

    system: datetime          # host system local time (DST of the OS)
    business: datetime        # same instant, in timezone_business
    business_day: date        # the business day this instant belongs to
    business_day_start_epoch: float  # epoch of this business day's cutoff (UTC seconds)

    # ---- Back-compat shim --------------------------------------------
    # Older code/tests reach for `.local`; keep it as an alias for the
    # business-TZ datetime (which is what every downstream caller now wants).
    @property
    def local(self) -> datetime:
        return self.business

    @property
    def weekday_business(self) -> int:
        return self.business_day.weekday()


def _parse_hhmm(raw: str) -> dt_time:
    h, m = raw.split(":", 1)
    return dt_time(hour=int(h), minute=int(m))


def _compute_business_day(
    business_dt: datetime, cutoff: dt_time, anchor: str
) -> date:
    """Map a business-TZ datetime to its business-day calendar date."""
    if business_dt.time() >= cutoff:
        # we are inside the window that opened today
        window_start_date = business_dt.date()
    else:
        # we are still inside yesterday's window
        window_start_date = business_dt.date() - timedelta(days=1)
    if anchor == "end":
        return window_start_date + timedelta(days=1)
    return window_start_date


def _business_day_start_dt(
    business_day: date, cutoff: dt_time, anchor: str, tz: ZoneInfo
) -> datetime:
    """The wall-clock instant at which the given business day's window opens,
    expressed as an aware datetime in timezone_business."""
    window_start_date = business_day - timedelta(days=1) if anchor == "end" else business_day
    return datetime.combine(window_start_date, cutoff, tzinfo=tz)


def now(cfg: AppConfig) -> ClockSnapshot:
    tz_business = ZoneInfo(cfg.timezone_business)
    cutoff = _parse_hhmm(cfg.business_day_start)
    system_local = datetime.now().astimezone()
    business = system_local.astimezone(tz_business)
    bday = _compute_business_day(business, cutoff, cfg.business_day_anchor)
    start_dt = _business_day_start_dt(bday, cutoff, cfg.business_day_anchor, tz_business)
    return ClockSnapshot(
        system=system_local,
        business=business,
        business_day=bday,
        business_day_start_epoch=start_dt.timestamp(),
    )


def business_day_start_epoch_for(cfg: AppConfig, business_day: date) -> float:
    """Public helper: cutoff epoch for any business day. Used by the
    watchdog + reconcile to filter stale success.txt files."""
    tz_business = ZoneInfo(cfg.timezone_business)
    cutoff = _parse_hhmm(cfg.business_day_start)
    start_dt = _business_day_start_dt(business_day, cutoff, cfg.business_day_anchor, tz_business)
    return start_dt.timestamp()


class Scheduler:
    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._day_start = _parse_hhmm(cfg.business_day_window["start"])
        self._day_end = _parse_hhmm(cfg.business_day_window["end"])
        self._day_interval = int(cfg.business_day_window["interval_seconds"])
        self._night_interval = int(cfg.business_night_window["interval_seconds"])

    def should_run_today(self, clock: ClockSnapshot) -> bool:
        return clock.weekday_business in self._cfg.active_weekdays

    def interval_seconds(self, clock: ClockSnapshot) -> int:
        if self._cfg.send_mode == "every_minute":
            return self._cfg.send_interval_seconds
        biz_clock = clock.business.time()
        if self._day_start <= biz_clock < self._day_end:
            return self._day_interval
        return self._night_interval

    def is_daily_reset_window(self, clock: ClockSnapshot, last_reset_date) -> bool:
        """Fires whenever the business day differs from the last reset.

        The cutoff is baked into ``clock.business_day`` itself, so this is a
        plain date comparison — no further HH:MM check needed.
        """
        return last_reset_date != clock.business_day
