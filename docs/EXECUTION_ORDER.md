# Execution order

This document traces, step by step, what the process does from launch to
the first transmitted payload, and exactly what happens on every
subsequent cycle.

## Startup sequence (Task Scheduler → first `/prices` POST)

### Phase 1 — bootstrap (sub-second)

| # | Step                                                                                    | File                  |
|---|-----------------------------------------------------------------------------------------|-----------------------|
| 1 | Task Scheduler runs `scripts\start_sender.bat` as SYSTEM.                               | `start_sender.bat`    |
| 2 | `.bat` activates venv and runs `python -m src.main`.                                    | `start_sender.bat`    |
| 3 | `main.main()` calls `load_config()`.                                                    | `src/main.py`         |
| 4 | `load_config()` loads `.env`, `config.json`, `price_groups.json`.                        | `src/config_loader.py`|
| 5 | `_build_price_groups()` builds the active price groups. Shared jobs (same job listed under multiple groups) are logged as INFO and tolerated — a single success.txt fans out to every group that lists it. | `src/config_loader.py`|
| 6 | `setup_logging()` creates `logs/sender-YYYY-MM-DD.log` + stdout handler.                 | `src/logger_setup.py` |
| 7 | `Sender.__init__()` constructs: StateManager, BackupManager, CsvBuilder, Transmitter, Scheduler, WatchdogMonitor, Heartbeat. | `src/main.py` |
| 8 | `StateManager.__init__()` **reads any `state.json` left by a crashed prior run** — survives restarts. | `src/state_manager.py`|

### Phase 2 — daily-reset alignment

| # | Step                                                                                    |
|---|-----------------------------------------------------------------------------------------|
| 9 | `perform_daily_reset()` is called **unconditionally once at startup** with today's local date. This is idempotent. |
|10 | State is cleared in-memory **and** `state.json` is deleted.                              |
|11 | `BackupManager.rotate_for_new_day(today)` purges any `pricecapture_backup_<date>.csv` older than the retention window (default: today + yesterday). No rename — the filename carries the date. |
|12 | `BackupManager.purge_old_logs()` deletes every `sender-*.log` except today's + yesterday's. |

> Why reset at startup? Because Task Scheduler may relaunch us at 02:05
> after a crash — and we MUST ensure the new day starts clean, not with
> yesterday's in-memory state accidentally carried over.

### Phase 3 — watchdog + heartbeat

| # | Step                                                                                    |
|---|-----------------------------------------------------------------------------------------|
|13 | `WatchdogMonitor.reconcile_existing_files(active_jobs)` scans every `<PRICES_ROOT>/<job>/GetPricesResult/` directory once, calling `state.record()` for any pre-existing `success.txt`. |
|14 | `WatchdogMonitor.start(active_jobs)` schedules one event handler per watch directory and starts the `Observer` daemon thread. Missing directories are logged and skipped. |
|15 | `Heartbeat.start()` spawns the watchdog-of-the-watchdog daemon thread. |

### Phase 4 — main loop (steady state)

The main thread now enters `_run_loop()`. On each iteration:

| # | Step                                                                                    |
|---|-----------------------------------------------------------------------------------------|
|16 | `heartbeat.tick()` — refreshes the "last alive" timestamp.                               |
|17 | `clock_now(cfg)` builds a `ClockSnapshot(system, business, business_day, business_day_start_epoch)`. The business day is computed from `BUSINESS_TIMEZONE` + `BUSINESS_DAY_START` + `BUSINESS_DAY_ANCHOR` (defaults Europe/London, 06:00, start). |
|18 | **If inactive weekday** (business-day weekday ∉ `ACTIVE_WEEKDAYS`) → `stop.wait(60)` and loop. |
|19 | **If business day rolled over since last reset** → call `perform_daily_reset(today=clock.business_day)`. Rollover instant is `BUSINESS_DAY_START` in `BUSINESS_TIMEZONE` (e.g. 06:00 UK, DST-aware), NOT system midnight. |
|20 | `CsvBuilder.build(active_price_groups)` walks every active price group (grid mode):     |
|   | &nbsp;&nbsp;&nbsp;&nbsp;a. `StateManager.completion_ts_for(jobs, match_mode=pg.match_mode)` decides the row's timestamp. |
|   | &nbsp;&nbsp;&nbsp;&nbsp;b. `match_mode="all"` (default): MAX of all jobs, only when every job has a record; else `None`. |
|   | &nbsp;&nbsp;&nbsp;&nbsp;c. `match_mode="any"` (e.g. `JSE1/JSE`): MAX over jobs that have arrived; `None` only when no listed job has been seen. |
|   | &nbsp;&nbsp;&nbsp;&nbsp;d. Empty `jobs=[]` (audit-only rows): always returns `None` — row emitted with the empty-timestamp token. |
|21 | `BackupManager.write_today(payload, today)` — writes the full snapshot to `backups/pricecapture_backup_<today>.csv` (overwrites on every cycle). |
|22 | `Transmitter.send(payload, cycle_id)` — HTTP POST to `RECEIVER_URL`. Single attempt; failures are logged and the next cycle re-sends. |
|23 | `Scheduler.interval_seconds(clock)` — returns 60 (every_minute mode) or 600/120 (business_schedule — day window 06:00–22:00 UK at 10 min, night 2 min, all in `BUSINESS_TIMEZONE`). |
|24 | `stop.wait(interval)` — sleeps until next cycle or a shutdown signal.                    |

### Phase 5 — shutdown

Triggered by SIGINT / SIGTERM or a fatal exception in the loop.

| # | Step                                                                                    |
|---|-----------------------------------------------------------------------------------------|
|25 | `Heartbeat.stop()` — sets stop event; daemon thread exits.                               |
|26 | `WatchdogMonitor.stop()` — stops Observer, joins (5 s timeout).                          |
|27 | `Transmitter.close()` — closes requests Session.                                          |
|28 | Process exits with status 0 (clean), 1 (loop error), 2 (config), or 99 (heartbeat self-kill). |

## Timing diagram — a single completion event

```
t=00:00.000   success.txt created by RANTask
t=00:00.003   OS event → watchdog Observer
t=00:00.005   _SuccessFileHandler.on_created → os.stat → state.record()
t=00:00.007   state.json atomic write complete
              ─ job is now "recorded" but NOT yet transmitted ─
t=00:45.000   main loop wakes (mid-cycle)
t=00:45.001   CsvBuilder.build() → new row appears in snapshot
t=00:45.005   pricecapture_backup_<today>.csv updated on disk
t=00:45.050   HTTP POST → receiver returns 200
t=00:45.051   log: "CYCLE <id> | complete=N+1 | pending=M | tx_ok=True"
```

Worst-case latency between `success.txt` appearing and the receiver seeing
it is **one send interval** — 60 s in every-minute mode, 10 min in
business_schedule day window, 2 min in business_schedule night window.

## What happens when the receiver is down

1. Cycle 1: POST fails → log warning → `pricecapture_backup_<today>.csv` still written locally.
2. Cycle 2 (60 s later): **same full snapshot** POSTs again.
3. Any number of failed cycles are safe — the payload is stateless from
   the sender's perspective.
4. When the receiver comes back, the very next cycle delivers everything.
5. **No queue builds up. No retry storm. No data loss** (because
   `state.json` is still on disk and RANTask doesn't re-create
   `success.txt` until the next business day).
