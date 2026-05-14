# Deployment Guide

This document is organized by **what you're trying to do**, not by theory.
Find the scenario that matches your situation and follow the steps.

---

## Scenarios at a glance

| I want to...                                              | Go to |
| --------------------------------------------------------- | ----- |
| Test the sender manually on a server for the first time   | [Scenario A](#scenario-a--first-manual-run-on-the-sender-server) |
| Use my laptop as a temporary receiver                      | [Scenario B](#scenario-b--laptop-as-temporary-receiver)          |
| Run the sender 24×7, unattended, surviving reboots         | [Scenario C](#scenario-c--247-production-on-the-sender-server)   |
| Run an always-on receiver on a Linux box                   | [Scenario D](#scenario-d--always-on-receiver-on-a-linux-box)     |
| Cut over to the real automation-portal endpoint            | [Scenario E](#scenario-e--cut-over-to-the-real-portal-endpoint)  |
| Troubleshoot something                                     | [Troubleshooting](#troubleshooting)                              |

---

## Prerequisites (once per machine)

### Windows sender server (e.g. `gbvinpsp-004`, `ukvinpsp-004`)

- Python 3.13 from python.org — tick **"Add to PATH"** during install.
- Administrator access (only needed when you install as a scheduled task).
- Network route from this server to the receiver's host:port. Test with:
  ```powershell
  Test-NetConnection -ComputerName <receiver-ip> -Port 8080
  ```
  Must say `TcpTestSucceeded : True`.

### Linux / macOS receiver

- Python 3.10 or later.
- Port the receiver will listen on (default 8080) **open** in the OS firewall.

---

## Scenario A — First manual run on the sender server

**Goal:** verify the sender starts, reads config, and POSTs something
to a receiver you control. Takes ~10 minutes. No Task Scheduler, no
SYSTEM account — just you at a PowerShell prompt.

### A.1 Copy the project onto the server

Copy the `price_monitor_sender/` folder to, say, `C:\pricesender\`.
USB, SCP, RDP clipboard, network share — whatever works at your site.

### A.2 Create venv and install dependencies

Open **PowerShell** (no admin needed), then:

```powershell
cd C:\pricesender
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

You should now see `(.venv)` in your prompt.

### A.3 Configure `.env`

```powershell
copy .env.example .env
notepad .env
```

At minimum set these five:

```ini
RECEIVER_URL=http://<receiver-ip>:8080/prices
SENDER_HOSTNAME=<THIS-SERVER-HOSTNAME>
SENDER_ENV=DR
SENDER_SITE=UKPROD          # or USPROD on the US box
PRICES_ROOT=C:\Prices
```

`SENDER_SITE` is **required** — the sender refuses to start without
`UKPROD` or `USPROD`. UK box → `UKPROD`. US box (the one running
CME_SPAN* and OCC* RANTask jobs) → `USPROD`.

For a first test you probably want:

```ini
ACTIVE_WEEKDAYS=0,1,2,3,4,5,6     # so weekends don't block the test
SEND_INTERVAL_SECONDS=60
```

Everything else — keep defaults. See `.env.example` for what each
variable means.

### A.4 Sanity-check the config loads

```powershell
python -c "from src.config_loader import load_config; print('active groups:', len(load_config().active_price_groups))"
```

Expect: `active groups: 187`. If you get an exception, the `.env` or
`config/price_groups.json` is broken — fix before going further.

### A.5 Run it

```powershell
python -m src.main
```

The process stays in the foreground and prints a cycle line every
`SEND_INTERVAL_SECONDS`:

```
=== PRICE MONITOR SENDER START | site=UKPROD | env=DR | host=gbvinpsp-004 | mode=every_minute | price_groups=187 ===
Loaded 12 audit-only price group(s) with no jobs (always emitted with empty timestamp — manual-fill prices, KSE clients, etc.)
site=UKPROD | always-empty rows on this host: 10 (pure cross-site composites: 0) | the receiver fills these from the other site's CSV
site=UKPROD | skipping watcher attach for the cross-site job(s) (paths live on the OTHER host)
Watchdog observer started | watching=182 | missing_dirs=0
CYCLE abc123 | site=UKPROD | rows=187 | complete=0 | pending=187 | tx_ok=True | bytes=3450
```

Stop with **Ctrl+C**.

### A.6 Prove it end-to-end with a fake success file

In a second PowerShell window on the same server:

```powershell
New-Item -ItemType Directory -Path C:\Prices\NSE_IX_1\GetPricesResult -Force
New-Item -ItemType File      -Path C:\Prices\NSE_IX_1\GetPricesResult\success.txt -Force
```

Within one cycle the sender's log shows `RECORDED NSE_IX_1`, and the
next transmission includes the row `NSE_IX_1|<date> <time>`. That's
your green light.

---

## Scenario B — Laptop as temporary receiver

**Goal:** use your own Mac/Linux laptop as a stand-in receiver while
the real automation-portal endpoint is being built. **Good for
testing**, not a long-term answer (laptops sleep).

### B.1 Start the receiver

```bash
cd /path/to/price_monitor_sender
./scripts/run_dashboard.sh 0.0.0.0 8080
```

The `0.0.0.0` is important — it tells Flask to accept connections from
other machines, not only from localhost.

The first launch creates `.venv` and installs Flask. Subsequent
launches are instant.

You'll see:

```
 * Running on http://0.0.0.0:8080
```

### B.2 Open the dashboard

In your browser: `http://localhost:8080/`

Every CSV the sender POSTs appears here in near-realtime with sender
hostname, cycle ID, byte count, and the parsed rows.

### B.3 Tell the sender where to send

Find your laptop's LAN IP:

```bash
# macOS / Linux
ifconfig | grep "inet " | grep -v 127.0.0.1
```

Pick the `192.168.x.x`, `10.x.x.x`, or `172.16-31.x.x` address. Put it
in `RECEIVER_URL` on the sender server:

```ini
RECEIVER_URL=http://192.168.1.42:8080/prices
```

Restart the sender (`Ctrl+C`, then `python -m src.main` again).

### B.4 Keep the laptop awake while testing (macOS)

```bash
caffeinate -d -i ./scripts/run_dashboard.sh 0.0.0.0 8080
```

`caffeinate -d -i` prevents display and system sleep for as long as the
receiver is running.

### B.5 Gotchas

- **macOS firewall popup** — first time Python listens on
  `0.0.0.0:8080` you'll get "Allow incoming connections?". Click
  **Allow** or nothing reaches you.
- **Corporate VPN** — the sender server and your laptop must be on
  routable networks. If `Test-NetConnection` from the server can't
  reach your laptop's IP, no code change will help.
- **When your laptop sleeps** the sender's POSTs fail. No data is
  lost — `state.json` on the server persists. When the laptop wakes,
  the next cycle (up to 60s later) delivers the full snapshot.

---

## Scenario C — 24×7 production on the sender server

**Goal:** the sender runs unattended under Windows Task Scheduler,
auto-starts on reboot, auto-restarts on crash, runs Mon-Fri only,
performs its daily reset at 06:00 UK time (BST/GMT aware).

Prerequisite: Scenario A works end-to-end when run manually. Do not
schedule a broken sender.

### C.1 Tighten `.env` for production

```ini
SENDER_ENV=PROD
ACTIVE_WEEKDAYS=0,1,2,3,4          # Mon-Fri only
SEND_MODE=every_minute
SEND_INTERVAL_SECONDS=60
# Business-day cutoff = daily reset moment. UK time, DST/BST aware.
# Works correctly on a US-system-time host (USPROD): the rollover still
# fires at 06:00 UK, and CSV timestamps still render in UK clock.
BUSINESS_TIMEZONE=Europe/London
BUSINESS_DAY_START=06:00
BUSINESS_DAY_ANCHOR=start
LOG_LEVEL=INFO
```

### C.2 Register the scheduled task (one-time, as Administrator)

Open **PowerShell as Administrator**, then:

```powershell
cd C:\pricesender
powershell -ExecutionPolicy Bypass -File scripts\install_task_scheduler.ps1
```

This creates a task called **PriceMonitorSender** with three triggers
and a set of failure-recovery rules.

What the script wires up and why:

| Setting                              | Value                                   | Purpose |
| ------------------------------------ | --------------------------------------- | ------- |
| Trigger 1: AtStartup                  | Fires when server boots                  | Reboot → sender restarts without human intervention |
| Trigger 2: Mon-Fri 05:55 LOCAL        | Fires at 05:55 every weekday             | Guarantees a fresh process is running across the 06:00-UK business-day-rollover window. Adjust local time for non-UK hosts (see install_task_scheduler.ps1 comments). |
| Trigger 3: every 5 minutes (safety net) | Repeats for 365 days                     | If the process has exited, Task Scheduler re-invokes it within 5 min |
| `MultipleInstances IgnoreNew`         | Blocks duplicate launches                | The 5-min trigger cannot spawn a second copy while one is already running |
| `RestartCount 3, RestartInterval 1 min` | Three 1-min retries on crash            | Recovers from transient Python errors fast |
| `NT AUTHORITY\SYSTEM`, `RunLevel Highest` | Runs as SYSTEM, elevated                | No logged-in user required, full read access to `C:\Prices` |
| `ExecutionTimeLimit 0`                | No timeout                               | Long-lived service won't be killed for running too long |
| `AllowStartIfOnBatteries`             | Yes                                      | Belt-and-braces for laptop/DR use |

### C.3 Start it now (don't wait for reboot or the next 5-min slot)

```powershell
Start-ScheduledTask -TaskName PriceMonitorSender
```

### C.4 Verify it's alive

```powershell
# Task state and last result
Get-ScheduledTask -TaskName PriceMonitorSender | Get-ScheduledTaskInfo

# Python process running?
Get-Process python

# Today's log, tailed live
Get-Content -Wait logs\sender-$(Get-Date -f yyyy-MM-dd).log
```

Expected log content on a healthy startup:

```
=== PRICE MONITOR SENDER START | env=PROD | host=gbvinpsp-004
=== DAILY RESET BEGIN ...
Startup reconciliation complete — N new records captured
Watchdog observer started | watching=199 | missing_dirs=0
CYCLE abc123 | complete=0 | pending=187 | tx_ok=True | bytes=3450
```

### C.5 Things 24×7 handles automatically (don't worry about these)

| Concern                                              | How it's handled |
| ---------------------------------------------------- | ---------------- |
| Log files growing forever                             | Daily rotation on business-day rollover (`BUSINESS_DAY_START` in `BUSINESS_TIMEZONE`); yesterday kept, older purged |
| State after a crash                                   | `state.json` atomically persisted on every detection; restart resumes today's state |
| `success.txt` files that appeared during downtime     | `reconcile_and_watch()` rescans every loop; pre-existing files are picked up |
| Process hang                                          | Heartbeat watchdog self-kills after `HEARTBEAT_MAX_SILENCE_SECONDS`; Task Scheduler relaunches |
| Server reboot                                         | `AtStartup` trigger brings it back before anyone logs in |
| Day rollover                                          | At `BUSINESS_DAY_START` in `BUSINESS_TIMEZONE` (default 06:00 UK, DST/BST aware): state wiped, new `pricecapture_backup_<business-day>.csv` starts, files outside retention window purged, log rolled |
| Weekend quiet                                         | `ACTIVE_WEEKDAYS=0,1,2,3,4` mask + Task Scheduler Mon-Fri trigger |

### C.6 Stop / uninstall

```powershell
# Temporary stop — task still registered
Stop-ScheduledTask -TaskName PriceMonitorSender

# Permanent removal
Unregister-ScheduledTask -TaskName PriceMonitorSender -Confirm:$false
Stop-Process -Name python -Force -ErrorAction SilentlyContinue
```

---

## Scenario D — Always-on receiver on a Linux box

**Goal:** replace the laptop-as-receiver with a real always-on Linux
server so the sender never misses a cycle. Recommended once manual
testing is done and you're waiting for the real portal endpoint.

### D.1 Deploy the receiver

On the Linux box:

```bash
git clone <repo-url> /opt/pricesender-receiver
cd /opt/pricesender-receiver
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Open firewall (choose port 80 if you want to drop the :port in URLs)
sudo ufw allow 8080/tcp     # or: firewall-cmd --permanent --add-port=8080/tcp
```

### D.2 Systemd unit (auto-start, auto-restart)

Create `/etc/systemd/system/pricesender-receiver.service`:

```ini
[Unit]
Description=Price Monitor Sender - Mock Receiver (staging)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pricesender
WorkingDirectory=/opt/pricesender-receiver
ExecStart=/opt/pricesender-receiver/.venv/bin/python -m dashboard.mock_receiver --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
StandardOutput=append:/var/log/pricesender-receiver.log
StandardError=append:/var/log/pricesender-receiver.log

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo useradd -r -s /usr/sbin/nologin pricesender       # one-time
sudo chown -R pricesender:pricesender /opt/pricesender-receiver
sudo systemctl daemon-reload
sudo systemctl enable pricesender-receiver
sudo systemctl start pricesender-receiver
sudo systemctl status pricesender-receiver
```

### D.3 Point the sender at it

In `.env` on the sender server:

```ini
RECEIVER_URL=http://<linux-box-ip>:8080/prices
```

Restart the scheduled task (`Stop-ScheduledTask` then `Start-ScheduledTask`).

---

## Scenario E — Cut over to the real portal endpoint

**Goal:** once the portal team has `ukvinpbs-101` listening on `/xyz`,
swap from your staging receiver to the real one. Zero code change.

### E.1 Ask the portal team for three things

1. **Hostname or IP** of `ukvinpbs-101` as visible from the sender network.
2. **Exact path** of the receive endpoint (confirmed `/xyz`).
3. **Protocol** — HTTP or HTTPS, and the port.

### E.2 Change one line in `.env`

```ini
RECEIVER_URL=http://y.y.y.y/xyz
# or, if TLS-terminated:
# RECEIVER_URL=https://ukvinpbs-101.internal/xyz
```

### E.3 Restart the scheduled task

```powershell
Stop-ScheduledTask  -TaskName PriceMonitorSender
Start-ScheduledTask -TaskName PriceMonitorSender
```

Next cycle, logs should show `status=200` against the new URL. That's
it — the sender is transport-agnostic.

### E.4 If the portal returns something other than 200

The sender logs `Transmission FAIL` and retries every cycle. It does
NOT drop data — `state.json` still has every detection. Work with the
portal team to figure out why; the next successful cycle will deliver
the full current snapshot.

---

## End-to-end test scenarios

Use this table once you have **sender running + receiver running + a
mock PRICES_ROOT** to confirm every spec behavior.

On the sender side (Windows), replace `simulate_success.sh` with the
PowerShell equivalent:

```powershell
function New-Success($job) {
  New-Item -ItemType Directory -Path "C:\Prices\$job\GetPricesResult" -Force | Out-Null
  New-Item -ItemType File      -Path "C:\Prices\$job\GetPricesResult\success.txt" -Force | Out-Null
}
```

| Scenario                         | How to simulate                              | Expected result |
| -------------------------------- | -------------------------------------------- | --------------- |
| Single-job completion             | `New-Success NSE_IX_1`                        | Row `NSE_IX_1\|dd/mm/yyyy hh:mm:ss` in next payload |
| Partial composite                 | `New-Success ICE_GSPD`                        | No row for the composite yet (spec: do not send partial) |
| Full composite                    | `New-Success ICE_GSPD; New-Success ICE_GPDR; New-Success ICEUS_GSPD` | One row `ICE_GSPD / ICE_GPDR / ICEUS_GSPD\|<MAX timestamp>` |
| PATH composite with shared job    | `New-Success ATHFIX; New-Success ATHVCT; New-Success ATHISIN` | Two rows: `PATH\|...` AND `IATH\|...` (ATHISIN feeds both, intentional overlap) |
| Duplicate re-run same day         | Delete + recreate `success.txt`               | Timestamp does NOT change (first-detection-locked, Q18) |
| Receiver down                     | Stop the receiver                             | Sender logs `Transmission FAIL`; next cycle retries automatically |
| Process crash                     | `Stop-Process -Name python -Force`            | `state.json` survives; scheduled task relaunches within 5 min; reconciliation picks up pre-existing files |
| Daily reset                       | Edit `BUSINESS_DAY_START` to "00:01", restart, wait across UK midnight | State cleared, new `pricecapture_backup_<business-day>.csv` starts, yesterday's file preserved, older backups + logs purged |
| Weekend gating                    | Set `ACTIVE_WEEKDAYS` to a weekday that is NOT today, restart | Loop sleeps silently; no payloads sent |

---

## Troubleshooting

| Symptom                                                      | Likely cause and fix                                                |
| ------------------------------------------------------------ | ------------------------------------------------------------------- |
| Sender exits immediately with code 2                          | `RECEIVER_URL` is missing or still the placeholder — edit `.env` |
| `KeyError: 'RECEIVER_URL'`                                    | `.env` wasn't loaded — confirm it's in the project root, not `config/` |
| `ValueError: ACTIVE_WEEKDAYS value out of range`              | Typo in `.env` — values must be 0-6 |
| Watchdog starts with `watching=0`                              | `PRICES_ROOT` is wrong or directory is empty — sender runs anyway, reconciliation catches new dirs next cycle |
| Payload always arrives empty                                  | No active price group has had a `success.txt` detected yet — check `logs/sender-*.log` for `RECORDED` lines |
| `Transmission FAIL` every cycle                                | Receiver unreachable. From the sender run: `Test-NetConnection -ComputerName <ip> -Port <port>` |
| Dashboard never shows rows but sender says `tx_ok=True`        | Receiver endpoint path mismatch — `RECEIVER_URL=.../prices` must match receiver's `/prices` route |
| Task Scheduler shows "Last Run Result: 0x1" / 0x2              | venv missing — re-run `py -3.13 -m venv .venv && .venv\Scripts\pip install -r requirements.txt` |
| Task Scheduler starts but process isn't visible                | It's running as SYSTEM — check `logs/sender-<date>.log` instead of Task Manager's "Apps" tab |
| `pricecapture_backup_<today>.csv` is empty but sender logs `complete=N>0` | You looked between cycles — file is overwritten each cycle, open it again after the next `CYCLE` line |
| Log file not rotating                                         | Rotation happens ONCE per day at `BUSINESS_DAY_START` in `BUSINESS_TIMEZONE`, not strictly at system midnight |
| "Operation not permitted" on macOS receiver                    | macOS firewall — System Settings → Network → Firewall → allow Python |

---

## Quick reference — file map

| File                                  | Edit it when...                                          |
| ------------------------------------- | -------------------------------------------------------- |
| `.env`                                 | Changing receiver URL, hostname, paths, scheduling mode |
| `config/price_groups.json`             | Adding/removing price groups, fixing job-to-group mapping |
| `config/config.json`                   | Changing CSV format, IST window breakpoints, log sizes (rare) |
| `scripts/install_task_scheduler.ps1`    | Changing task name, trigger schedule, user account (rare) |
| `scripts/start_sender.bat`              | Launcher — shouldn't need edits |
| `logs/sender-YYYY-MM-DD.log`            | Read-only — today's runtime log |
| `state/state.json`                     | Never edit by hand. Delete it manually only if you want to force a full reset |
| `backups/pricecapture_backup_YYYY-MM-DD.csv` | Read-only — today's + yesterday's last successful CSV payloads (filename carries the date) |
