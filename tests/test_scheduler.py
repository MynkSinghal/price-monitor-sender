from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.scheduler import (
    ClockSnapshot,
    Scheduler,
    _compute_business_day,
    business_day_start_epoch_for,
)


def _make_cfg(mode: str = "business_schedule", anchor: str = "start"):
    return SimpleNamespace(
        send_mode=mode,
        send_interval_seconds=60,
        active_weekdays=(0, 1, 2, 3, 4),
        timezone_business="Europe/London",
        business_day_start="06:00",
        business_day_anchor=anchor,
        business_day_window={"start": "06:00", "end": "22:00", "interval_seconds": 600},
        business_night_window={"interval_seconds": 120},
    )


def _snapshot(uk_hour: int, uk_minute: int = 0, weekday: int = 2) -> ClockSnapshot:
    tz = ZoneInfo("Europe/London")
    # Wed 16 Jul 2025 is weekday=2; shift to land on the requested weekday.
    base = datetime(2025, 7, 16 + (weekday - 2), uk_hour, uk_minute, tzinfo=tz)
    bday = _compute_business_day(base, datetime(2000, 1, 1, 6, 0).time(), "start")
    return ClockSnapshot(
        system=base.astimezone(),
        business=base,
        business_day=bday,
        business_day_start_epoch=base.timestamp(),
    )


def test_day_interval_at_10_uk() -> None:
    sched = Scheduler(_make_cfg())
    assert sched.interval_seconds(_snapshot(10, 0)) == 600


def test_night_interval_at_03_uk() -> None:
    sched = Scheduler(_make_cfg())
    assert sched.interval_seconds(_snapshot(3, 0)) == 120


def test_boundary_22_is_night() -> None:
    sched = Scheduler(_make_cfg())
    assert sched.interval_seconds(_snapshot(22, 0)) == 120


def test_every_minute_mode_is_60s_regardless_of_time() -> None:
    sched = Scheduler(_make_cfg(mode="every_minute"))
    assert sched.interval_seconds(_snapshot(3, 0)) == 60
    assert sched.interval_seconds(_snapshot(10, 0)) == 60
    assert sched.interval_seconds(_snapshot(22, 0)) == 60


def test_weekend_is_inactive() -> None:
    sched = Scheduler(_make_cfg())
    assert not sched.should_run_today(_snapshot(10, 0, weekday=5))
    assert not sched.should_run_today(_snapshot(10, 0, weekday=6))
    assert sched.should_run_today(_snapshot(10, 0, weekday=0))


# ---- business-day boundary ------------------------------------------------

def test_business_day_before_cutoff_belongs_to_yesterday() -> None:
    tz = ZoneInfo("Europe/London")
    cutoff = datetime(2000, 1, 1, 6, 0).time()
    # 04:30 UK on 2025-07-17 → still business day 2025-07-16 (anchor=start)
    bday = _compute_business_day(datetime(2025, 7, 17, 4, 30, tzinfo=tz), cutoff, "start")
    assert bday == date(2025, 7, 16)


def test_business_day_after_cutoff_belongs_to_today() -> None:
    tz = ZoneInfo("Europe/London")
    cutoff = datetime(2000, 1, 1, 6, 0).time()
    bday = _compute_business_day(datetime(2025, 7, 17, 7, 30, tzinfo=tz), cutoff, "start")
    assert bday == date(2025, 7, 17)


def test_business_day_anchor_end() -> None:
    tz = ZoneInfo("Europe/London")
    cutoff = datetime(2000, 1, 1, 6, 0).time()
    # 04:30 UK on 2025-07-17 → business day labelled 2025-07-17 with anchor=end
    bday = _compute_business_day(datetime(2025, 7, 17, 4, 30, tzinfo=tz), cutoff, "end")
    assert bday == date(2025, 7, 17)
    bday = _compute_business_day(datetime(2025, 7, 17, 7, 30, tzinfo=tz), cutoff, "end")
    assert bday == date(2025, 7, 18)


def test_business_day_start_epoch_is_06_uk_dst_aware() -> None:
    cfg = _make_cfg()
    # 2025-07-16 is BST (UTC+1), so 06:00 UK = 05:00 UTC.
    epoch = business_day_start_epoch_for(cfg, date(2025, 7, 16))
    expected = datetime(2025, 7, 16, 5, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    assert epoch == expected
    # 2025-01-15 is GMT (UTC+0), so 06:00 UK = 06:00 UTC.
    epoch = business_day_start_epoch_for(cfg, date(2025, 1, 15))
    expected = datetime(2025, 1, 15, 6, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    assert epoch == expected


def test_daily_reset_fires_on_business_day_change() -> None:
    sched = Scheduler(_make_cfg())
    snap = _snapshot(7, 0)
    assert sched.is_daily_reset_window(snap, last_reset_date=snap.business_day - timedelta(days=1))
    assert not sched.is_daily_reset_window(snap, last_reset_date=snap.business_day)
