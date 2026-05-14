# Price Monitor Sender

Production-grade Python 3.13 service that watches RANTask `success.txt` flags,
aggregates timestamps into composite price groups, and transmits CSV snapshots
to the automation portal receiver every cycle.

> This system is strictly a **sender**. All business logic, overrides,
> reconciliation, and backfills are handled by the receiver.

## Contents

- [Quick start](#quick-start)
- [Directory layout](#directory-layout)
- [Configuration](#configuration)
- [Running the sender](#running-the-sender)
- [Running the DR dashboard](#running-the-dr-dashboard)
- [Locked design decisions](#locked-design-decisions)
- [Documentation index](#documentation-index)

## Quick start

### Prod (Windows)

```powershell
# One-time setup
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# Edit .env: set RECEIVER_URL, PRICES_ROOT, SENDER_HOSTNAME

# Install the scheduled task (runs as SYSTEM, Mon-Fri, with self-restart)
powershell -ExecutionPolicy Bypass -File scripts\install_task_scheduler.ps1
```

### DR / local testing (Linux)

```bash
# Terminal 1 — mock receiver + dashboard on http://127.0.0.1:8080
./scripts/run_dashboard.sh

# Terminal 2 — create a mock price layout and run the sender
cp .env.example .env
# Edit .env: RECEIVER_URL=http://127.0.0.1:8080/prices, PRICES_ROOT=/tmp/mock-prices
./scripts/run_sender_local.sh

# Terminal 3 — simulate a RANTask completion
./scripts/simulate_success.sh /tmp/mock-prices NSE_IX_1 ICE_GSPD ICE_GPDR ICEUS_GSPD
# Dashboard will show the rows appear on the next cycle (<= 60s).
```

## Directory layout

```
price_monitor_sender/
├── config/
│   ├── config.json           non-secret runtime knobs (CSV format, timezones, intervals)
│   └── price_groups.json     definitive price group table (189 rows, 187 active)
├── src/
│   ├── main.py               entry point + orchestration loop
│   ├── config_loader.py      merges .env + JSON configs, enforces Q6 no-overlap
│   ├── state_manager.py      thread-safe store + atomic state.json persistence
│   ├── watchdog_monitor.py   Observer watching every <PRICES_ROOT>/<job>/GetPricesResult/
│   ├── csv_builder.py        builds "price_group_name | dd/mm/yyyy hh:mm:ss" snapshots
│   ├── transmitter.py        HTTP POST, 1 attempt per cycle (next cycle retries organically)
│   ├── scheduler.py          weekday mask + IST-based interval logic (DST-aware via zoneinfo)
│   ├── backup_manager.py     pricecapture_backup_<date>.csv rotation + log purge
│   ├── daily_reset.py        idempotent 02:00 reset
│   ├── heartbeat.py          self-kill watchdog (restart via Task Scheduler)
│   └── logger_setup.py       rotating daily log files
├── dashboard/
│   ├── mock_receiver.py      Flask app (mock receiver + DR dashboard in one)
│   ├── templates/dashboard.html
│   └── static/style.css
├── scripts/
│   ├── install_task_scheduler.ps1
│   ├── start_sender.bat
│   ├── run_sender_local.sh
│   ├── run_dashboard.sh
│   └── simulate_success.sh
├── tests/
│   ├── test_state_manager.py
│   ├── test_csv_builder.py
│   ├── test_scheduler.py
│   ├── test_backup_manager.py
│   └── test_config_loader.py
├── docs/
│   ├── ARCHITECTURE.md
│   ├── EXECUTION_ORDER.md
│   └── DEPLOYMENT.md
├── state/                    state.json lives here (atomic writes)
├── backups/                  pricecapture_backup_YYYY-MM-DD.csv (today + yesterday)
├── logs/                     sender-YYYY-MM-DD.log (rotated)
├── requirements.txt
└── .env.example
```

## Configuration

All tunable knobs live in three files. **Only `.env` holds secrets / per-deployment values.**

| Source                    | Purpose                                                  |
| ------------------------- | -------------------------------------------------------- |
| `.env`                    | Receiver URL, paths, sender identity, intervals         |
| `config/config.json`      | Non-secret runtime defaults (CSV format, scheduler)      |
| `config/price_groups.json`| Static price group → RANTask job mapping (loaded once)   |

`price_groups.json` has **189 rows** covering every entry from the scheduled
batch table (reconciled against `prciesdata.csv` on 2026-05-02):

- **187 active** rows are emitted in every CSV cycle.
- **2 inactive** rows are kept for audit only.

Active rows split into three flavours:

- **Normal** — one or more RANTask jobs; row carries a timestamp once **all** jobs
  have arrived (`match_mode: "all"`, the default). MAX(`st_ctime`) wins.
- **OR-mode** (`match_mode: "any"`) — the row flags as soon as **any** listed
  job arrives. Used by `JSE1/JSE` where either or both files may show on a
  given day; FIRST-arrival timestamp when only one is in, MAX when both are in.
- **Audit-only** (`jobs: []`) — included in every CSV cycle with an empty
  timestamp column. The receiver fills these by hand or from another source.
  Used for KSE clients (file picked by MM directly), manual-fill prices
  (`RCFT`, `PCFF`, `PDCE`), and rows whose schedule has no RANTask job
  (`PSTM / SSTM / POMT / SOMT`, `ISTM`). Currently 12 audit-only rows.

**Job overlap is expected and handled.** A success.txt at
`<PRICES_ROOT>/ATHISIN/GetPricesResult/` simultaneously satisfies the
`ATHISIN` slot in the `PATH` composite (ATHFIX + ATHVCT + ATHISIN) and
the `IATH` standalone group. Each price group is evaluated independently
each cycle; shared jobs are recorded once in state. Other intentional
overlaps include `CMEF` (feeds the long P/S row + `XCBT/XCME/XCMX/XNYM`),
`LME_DATA + LME_READY` (composite + `XLME`), and `FOX`/`LTM` (standalone
+ X-prefixed sister row).

**After any edit to a config file, restart the sender** (Q5 locked: static per run).

## Running the sender

The sender is a single long-lived Python process:

- Thread 1 (Watchdog): real-time `success.txt` detection via
  `ReadDirectoryChangesW` (Windows) / `inotify` (Linux).
- Thread 2 (Main loop): wakes every `SEND_INTERVAL_SECONDS`, builds CSV,
  POSTs to receiver, writes `backups/pricecapture_backup_<today>.csv`.
- Thread 3 (Heartbeat): monitors the main loop; `os._exit(99)` if stuck
  longer than `HEARTBEAT_MAX_SILENCE_SECONDS` so Task Scheduler relaunches.

```bash
# Manual invocation (useful for debugging)
.venv/bin/python -m src.main
```

Exit codes:

| Code | Meaning                                                      |
| ---: | ------------------------------------------------------------ |
| 0    | Graceful shutdown (SIGINT / SIGTERM)                          |
| 1    | Fatal error in main loop (details in log)                    |
| 2    | Config error (typically missing `RECEIVER_URL`)              |
| 99   | Heartbeat self-kill — process was stuck                      |

## Running the DR dashboard

```bash
./scripts/run_dashboard.sh 0.0.0.0 8080
```

Open `http://localhost:8080/` in a browser. The page auto-refreshes every
10 s and shows:

- Latest received payload (cycle ID, sender hostname, bytes, row count)
- Parsed rows with a `composite` / `single` badge
- Raw CSV text

API endpoints:

- `POST /prices` — receives CSV (returns `HTTP 200`).
- `GET  /api/latest` — JSON of the most recent payload.
- `GET  /api/history?limit=N` — JSON list of the last `N` payloads.

## Two-site setup (UKPROD + USPROD)

We run **two** identical sender processes, one per site. Both POST to the same automation endpoint, and the receiver merges the two CSVs.

| | UKPROD | USPROD |
|---|---|---|
| `.env` `SENDER_SITE` | `UKPROD` | `USPROD` |
| Local jobs | every job NOT listed in `config/config.json → sites.usprod_jobs` | the 11 USPROD jobs (`CME_SPAN2A`, `CME_SPAN2I`, `CME_SPANE`, `CME_SPN_AI`, `CME_SPN_BE`, `CME_SPAN2S`, `OCC_CPM`, `OCCP`, `OCCP_NON`, `OCCS`, `OCCSSTD`) |
| HTTP header on every POST | `X-Sender-Site: UKPROD` | `X-Sender-Site: USPROD` |

Each cycle, **every active price group** is on the wire — completed groups carry a timestamp, groups not yet complete on this host carry the empty token (`config.json → csv.empty_timestamp_token`, default blank, configurable to `pending` / `NA`). The receiver merges per row by taking whichever sender filled in the timestamp.

```text
# UKPROD payload (excerpt)              # USPROD payload (same row order)
NSE_IX_1|23/04/2026 09:15:42            NSE_IX_1|
PATH|23/04/2026 17:05:33                PATH|
CME_SPAN2A|                             CME_SPAN2A|23/04/2026 15:00:11
OCC_CPM|                                OCC_CPM|23/04/2026 23:50:04
RCFT|                                   RCFT|                          # audit-only on both sides
JSE1/JSE|23/04/2026 14:02:11            JSE1/JSE|                      # OR-mode, UK has JSE
```

The receiver merges per row by taking whichever sender filled in a non-empty timestamp (and MAX if both sides happen to have it).

The list of cross-site jobs is **maintenance-only**: disks aren’t shared between UKPROD and USPROD, so the OS already enforces which `success.txt` files appear where. The list lets the sender (1) skip pointless watcher attaches for jobs that physically don’t exist on this host, and (2) print a one-line startup sanity log:

```text
site=UKPROD | always-empty rows on this host: 10 (pure cross-site composites: 0) | the receiver fills these from the other site's CSV
site=UKPROD | skipping watcher attach for the cross-site job(s) (paths live on the OTHER host)
```

## Locked design decisions

Reference for auditors — every one of the 27 questions + OBS items locked
in the clarifying conversation is implemented exactly as agreed.

| #      | Topic                      | Decision                                                            |
| ------ | -------------------------- | ------------------------------------------------------------------- |
| Q1     | Transport                  | HTTP POST                                                           |
| Q2     | Auth                       | None                                                                |
| Q3     | Success signal             | HTTP 200                                                            |
| Q4     | Price group config         | JSON file (`config/price_groups.json`)                              |
| Q5     | Runtime changes            | Static per run — restart after edits                                 |
| Q6     | Job overlap                | ALLOWED — a single job can feed multiple price groups (e.g. `ATHISIN` → both `PATH` composite and `IATH` standalone). The state manager records each job once and every group that lists it reads from the same record. |
| Q-2026-05-A | Match mode             | Per-row `match_mode`: `"all"` (default — composite AND, MAX of all jobs) or `"any"` (OR — first-arrival when only one job is in, MAX when more arrive). Used for `JSE1/JSE`. |
| Q-2026-05-B | Audit-only rows        | `active: true, jobs: []` rows are emitted in every CSV cycle with an empty timestamp. Used for KSE clients, manual-fill prices, and rows with no RANTask job. Receiver fills these from another source. |
| Q-2026-05-C | TASE weekday split     | `TASE` flags Mon–Thu, `TASE_F` flags Fri. Both rows present every day; sender doesn't reconcile, receiver picks whichever has the timestamp. |
| Q7     | Startup mechanism          | Windows Task Scheduler                                              |
| Q8     | Daily reset                | At `BUSINESS_DAY_START` in `BUSINESS_TIMEZONE` (default 06:00 UK, DST/BST aware). Business day spans 06:00 UK → 06:00 UK; the CSV/state/backup filename are all stamped with this business day (not system date). Works correctly on a US-system-time host. |
| Q9     | Watchdog recovery          | Kill + relaunch via Task Scheduler                                  |
| Q10    | Logging                    | Rotating daily files                                                |
| Q11    | Alerting                   | Log only — no external alerting                                      |
| Q12    | Log retention              | Yesterday only                                                      |
| Q13    | File detection             | `watchdog` library (ReadDirectoryChangesW) + 1-minute send loop      |
| Q14    | Timestamp source           | `st_ctime` (Windows true creation time)                             |
| Q15    | Stale file guard           | Active: `success.txt` whose `st_ctime` predates the current business-day cutoff is silently ignored (logged as `STALE … ignoring`). Belt-and-braces in case RANTask leaves yesterday's file behind. |
| Q16    | Retry model                | No intra-cycle retries — next 60s cycle sends the full snapshot again |
| Q17    | Stuck process              | Heartbeat → `os._exit(99)` → Task Scheduler relaunches               |
| Q18    | Duplicate detection        | First detection is locked for the day                                |
| Q19    | Backup filenames           | `pricecapture_backup_YYYY-MM-DD.csv` (today + yesterday retained)   |
| Q20    | Holiday calendar           | Mon–Fri absolute — no exceptions                                     |
| Q21    | Multi-machine              | Single sender only                                                   |
| OBS-A  | No-job entries             | Excluded                                                            |
| OBS-B  | Eclipse-only entries       | Treated as normal price groups                                      |
| OBS-C  | Composite name in CSV      | Exact slash-separated string (e.g. `ICE_GSPD / ICE_GPDR / ICEUS_GSPD`) |
| OBS-D  | X-prefixed / N/A batch     | Excluded                                                            |
| OBS-E  | Friday-unavailable groups  | Always monitored                                                    |

## Documentation index

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — component diagram, data flow, concurrency model.
- [`docs/EXECUTION_ORDER.md`](docs/EXECUTION_ORDER.md) — exact startup + per-cycle steps.
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — Task Scheduler + Linux DR deploy instructions.

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

43 tests cover: first-detection locking, composite MAX timestamps,
partial-composite exclusion, OR-mode (match_mode=any) FIRST/MAX semantics,
audit-only row CSV emission, day/night interval boundaries, weekend gating,
backup rotation, Q6 overlap validation, two-site cross-site job filtering.

## Assumptions (explicit)

1. **Config is trusted.** `price_groups.json` is the source of truth for
   what gets emitted. Job overlap across active rows is allowed and
   logged at startup; audit-only rows (`jobs: []`) are emitted with an
   empty timestamp every cycle.
2. **Filesystem semantics.** On Windows, `os.stat().st_ctime` returns the
   true creation timestamp. On Linux it returns inode-change time — DR
   testing on Linux laptops will therefore show slightly different values
   (expected and documented).
3. **Clock correctness.** The sender trusts local system time. If the
   machine's clock is wrong, CSV timestamps will be wrong. Recommend
   `w32time` sync against an authoritative NTP server on the sender.
4. **Receiver is reachable.** On a total network outage the sender
   continues recording state and keeps trying every cycle; no payload
   is ever queued (deltaless model — Q16).
5. **No business logic.** The sender never mutates timestamps, never
   reorders rows, never de-duplicates beyond the first-detection lock,
   and never skips a row based on business rules. All that is the
   receiver's job.
