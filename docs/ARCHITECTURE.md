# Architecture

## Component diagram

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                 Price Monitor Sender Process             │
                    │                                                          │
  filesystem ─────► │  ┌───────────────┐   record()    ┌──────────────────┐   │
  events            │  │ WatchdogMon   │ ────────────► │  StateManager    │   │
  (success.txt)     │  │ Observer      │               │  (dict + RLock)  │   │
                    │  └───────────────┘               │  persists state  │   │
                    │                                   │  .json atomically│   │
                    │                                   └────────┬─────────┘   │
                    │                                            │             │
                    │  ┌─────────────────────────────────────────▼───────────┐ │
                    │  │            Main send loop (1 thread)                │ │
                    │  │                                                      │ │
  Scheduler ─────►  │  │  every N seconds:                                   │ │
  (IST/UK/weekday)  │  │    1. heartbeat.tick()                              │ │
                    │  │    2. maybe perform daily reset                     │ │
                    │  │    3. CsvBuilder.build(snapshot)                    │ │
                    │  │    4. BackupManager.write_today()                   │ │
                    │  │    5. Transmitter.send() ── HTTP POST ──►  receiver │ │
                    │  └──────────────────────────────────────────────────────┘ │
                    │                                                          │
                    │  ┌───────────────────────┐                                │
                    │  │ HeartbeatWatchdog     │  if silent > threshold:        │
                    │  │ (daemon thread)       │  → os._exit(99)                │
                    │  └───────────────────────┘    (Task Scheduler relaunches) │
                    └─────────────────────────────────────────────────────────┘
```

## Data flow — one completion event

1. **RANTask** creates `C:\Prices\<job>\GetPricesResult\success.txt`.
2. **OS** emits a `ReadDirectoryChangesW` event.
3. **`watchdog.Observer`** calls the registered `_SuccessFileHandler`.
4. Handler calls `os.stat()` to capture `st_ctime`.
5. Handler calls `StateManager.record(job, st_ctime, path)`.
   - First detection wins (Q18) — subsequent events are ignored.
6. State manager atomically persists `state.json` (temp file + `os.replace`).
7. Main loop's next cycle evaluates **every** active price group that
   lists that job (and emits all the others too, in grid mode):
   - Single-job group → row appears with that job's timestamp.
   - Composite (`match_mode: "all"`) → row appears only when EVERY listed
     job has a record; timestamp is MAX(st_ctime) across them.
   - OR-mode (`match_mode: "any"`, e.g. `JSE1/JSE`) → row appears as soon
     as the FIRST listed job arrives; if more arrive later, the row's
     timestamp updates to MAX of those that have been seen.
   - Audit-only (`jobs: []`) → row is emitted every cycle with an empty
     timestamp regardless of state.
   - If the job is shared (e.g. `ATHISIN` in both `PATH` and `IATH`),
     both groups see the record and both emit rows independently.

## Concurrency model

| Thread                    | Role                                    | Blocking?    |
| ------------------------- | --------------------------------------- | ------------ |
| main                      | Send loop (builds + POSTs)              | I/O-bound    |
| watchdog-observer         | OS event pump                           | Kernel-blocking |
| watchdog-emitter (N)      | per-directory emitters spawned by watchdog | Kernel-blocking |
| heartbeat-watchdog        | Polls tick timestamp every 5 s          | sleep(5)     |

**Shared state:** `StateManager._records` (dict) protected by one `threading.RLock`.
Reads return shallow copies so the main thread never iterates over a dict being mutated.

## Failure modes & handling

| Failure                                      | Handling                                                            |
| -------------------------------------------- | ------------------------------------------------------------------- |
| Receiver network down                         | Single POST attempt, log warning. Next cycle re-sends full snapshot. No queueing needed (deltaless model). |
| Receiver returns 5xx                          | Same — next cycle re-sends.                                         |
| `success.txt` vanishes between create + stat  | Log warning, skip. Next reconciliation sweep will pick it up if it reappears. |
| `state.json` corrupted at startup             | Log warning, start empty. No crash.                                 |
| Main loop wedged (network/DNS call hangs)     | Heartbeat triggers `os._exit(99)` after `HEARTBEAT_MAX_SILENCE_SECONDS`. Task Scheduler restarts. |
| Process crashes / box reboots                 | Task Scheduler's `-AtStartup` trigger relaunches. `state.json` is reloaded from disk. |
| Config file edited at runtime                 | Ignored until next restart (Q5 locked). Prevents half-applied updates. |
| Two active price groups share a job           | Allowed. Startup logs an INFO line listing shared jobs; each group is evaluated independently and reads the same underlying record. |

## CSV semantics (grid mode, two-site, three row flavours)

- **Grid mode:** every cycle emits **one row per `active: true` price group**
  in JSON order — both completed and not-yet-completed.
- **Deltaless:** the row content reflects the latest local truth, not a delta.
  The receiver always sees the current state.
- **Three row flavours**, set per row in `price_groups.json`:

  | `match_mode` | `jobs` | Behaviour |
  |---|---|---|
  | `"all"` (default) | one or more | Row carries a timestamp only when every listed job has arrived. Timestamp = `MAX(st_ctime)`. Used for every standard row, single or composite. |
  | `"any"` | two or more | Row carries a timestamp as soon as **any** listed job has arrived. Timestamp = `MAX(st_ctime)` over jobs that have arrived (FIRST-arrival when only one is in, MAX when both are in). Used by `JSE1/JSE`. |
  | n/a (audit-only) | `[]` | Row is in the CSV every cycle with an empty timestamp. The receiver fills it in. Used for KSE clients, manual-fill prices, and rows with no RANTask job. |

- **Incomplete rows** (no timestamp from this host) carry the configured
  `csv.empty_timestamp_token` (default blank). Switching to `"pending"` /
  `"NA"` is a one-line config edit.
- **`active: false` rows:** never on the wire — audit-trail only inside
  the JSON file.
- **Two-site merge:** UKPROD and USPROD send the same row set in the same
  order; the timestamp column is non-empty only on the host that saw the
  corresponding `success.txt`. The receiver keeps the latest CSV per
  `X-Sender-Site` and merges per row by picking whichever side has a
  non-empty timestamp (or MAX if both — only happens when a job genuinely
  exists on both boxes, which is rare).
- **Row format:** `price_group_name|<timestamp or empty token>\n` —
  no header line, single `|` delimiter.

## Timezone + DST correctness

The entire system is anchored to a **single configurable business
timezone** (`BUSINESS_TIMEZONE`, default `Europe/London`). Everything that
asks "what date / time is it?" — the daily reset cutoff, the CSV filename,
the state-file business day, the stale-success.txt guard, the log
filename, the log record timestamps, and the timestamps rendered into the
CSV payload — uses this single source.

This means: a USPROD sender running on a US system clock still cuts over
at 06:00 UK time, still names its backup `pricecapture_backup_<UK-business-day>.csv`,
and still renders timestamps in UK clock-time in the CSV. The host OS
timezone is irrelevant to the wire output.

- `datetime.now().astimezone()` captures the host wall clock; we then
  convert to `ZoneInfo(BUSINESS_TIMEZONE)` for all business logic.
- `Europe/London` toggles between GMT and BST twice a year. `zoneinfo`
  handles both automatically because we ship `tzdata` as a Python package
  (important on Windows, which lacks a system zoneinfo DB).
- CSV timestamps use `datetime.fromtimestamp(epoch, tz=ZoneInfo(BUSINESS_TIMEZONE))`,
  formatted `dd/mm/yyyy hh:mm:ss` (spec requirement).
- The business day is computed by `_compute_business_day()` in
  `src/scheduler.py`. With `BUSINESS_DAY_ANCHOR=start`, an instant at
  04:30 UK on the morning of day D is still business day D-1 (the next
  window has not opened yet).

## Why these library choices?

| Library          | Why                                                              |
| ---------------- | ---------------------------------------------------------------- |
| `watchdog`       | Windows-native `ReadDirectoryChangesW` + Linux `inotify` in one API. Q13. |
| `requests`       | Battle-tested HTTP client; connection pooling via `Session`.     |
| `zoneinfo`       | Standard-library IANA TZ (Python 3.9+). No extra deps for tz math. |
| `tzdata`         | Provides the IANA DB on Windows (otherwise `zoneinfo` has no data). |
| `python-dotenv`  | Loads `.env` without tying us to a specific framework.           |
| `flask` + `flask-cors` | Tiny footprint for the DR dashboard / mock receiver.       |
| `pytest` + `freezegun` | Standard test tooling.                                     |
