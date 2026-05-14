# Price Monitor Sender — Startup Guide

> **Who is this for?**
> Anyone who needs to start, stop, or test this project — no technical background required.
> Just follow the steps in order and copy-paste the commands exactly as written.

---

## What does this project do?

This project runs on **two Windows servers** (one in the UK, one in the US).
Each server watches a folder on disk. When price files arrive, it builds a CSV and sends it over the network to a receiver every 60 seconds.
The receiver combines both and shows a live dashboard.

```
UK Server (ukvinpsp-004)         US Server (usvinpsp-108)
      │                                  │
      │  sends CSV every 60 s            │  sends CSV every 60 s
      └──────────────┬───────────────────┘
                     ▼
           Receiver server (e.g. gbvinpba-101)
           Live dashboard: http://gbvinpba-101:8080
```

---

#One thing to double-check — telnet testing port 443 only confirms the TCP connection is open. Before going live, run this quick test from the sender machine to confirm the SSL certificate is trusted and the path exists:

```python
.venv\Scripts\python -c "import requests; r = requests.post('https://automation-portal-address/their-path', data='TEST', timeout=10); print(r.status_code, r.text[:200])"
```
## Before you start — one question

> **Is the receiver (automation portal) already running at a known URL?**

- **YES** → You only need to run the sender setup (Scenario A or B below).
  Set `RECEIVER_URL` in `.env` to that URL and you are done.
- **NO / Testing only** → You also need to start the mock receiver on a separate machine
  (see **Scenario C** at the bottom of this document).

---

---

# SCENARIO A — Production: Permanent sender on a Windows server

> Run this **once** on each sender server.
> After this, the sender starts automatically every time the server boots
> and restarts itself if it ever crashes.

---

## Step 1 — Open PowerShell as Administrator

On the Windows server, press `Start`, type `PowerShell`,
right-click **Windows PowerShell** → **Run as administrator**.

---

## Step 2 — Go to the project folder

```powershell
cd C:\price_monitor_sender
```

> If the project is somewhere else, change the path above to match.

---

## Step 3 — Find which Python command works on this server

Run each line below one at a time until one shows a version number:

```powershell
python --version
```
```powershell
python3 --version
```
```powershell
py --version
```

Use whichever one responded. Replace `python` in the next step with that command if different.

---

## Step 4 — Create the Python environment and install packages

> Do this **once only**. Skip to Step 5 if the `.venv` folder already exists.

```powershell
python -m venv .venv
```
```powershell
.venv\Scripts\pip install -r requirements.txt
```

You will see packages downloading and installing. Wait for it to finish.

---

## Step 5 — Fill in the configuration file

```powershell
notepad .env
```

Notepad opens. **Delete everything** in the file and paste the block below.
Then fill in the two values marked with `← CHANGE THIS`.

```
RECEIVER_URL=https://automation-uk.iongroup.com/prices    ← CHANGE THIS to the real receiver URL
RECEIVER_TIMEOUT_SECONDS=15
SENDER_HOSTNAME=ukvinpsp-004                              ← CHANGE THIS to this server's name
SENDER_ENV=PROD
SENDER_SITE=UKPROD
PRICES_ROOT=C:\Prices
SEND_MODE=every_minute
SEND_INTERVAL_SECONDS=60

# Business day = 06:00 UK -> 06:00 UK next day. DST/BST aware. Works
# correctly even on a US-system-time host (CSV always renders UK clock).
BUSINESS_TIMEZONE=Europe/London
BUSINESS_DAY_START=06:00
BUSINESS_DAY_ANCHOR=start

HEARTBEAT_MAX_SILENCE_SECONDS=300
LOG_LEVEL=INFO
ACTIVE_WEEKDAYS=0,1,2,3,4
```

> **If this is the US server (`usvinpsp-108`)**, change these two lines:
> ```
> SENDER_HOSTNAME=usvinpsp-108
> SENDER_SITE=USPROD
> ```
> Everything else stays the same.

Save the file (`Ctrl+S`) and close Notepad.

---

## Step 6 — Register the permanent scheduled task

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_task_scheduler.ps1
```

This registers the sender with Windows Task Scheduler so it:
- Starts automatically when the server boots
- Restarts automatically if it crashes
- Never runs more than one copy of itself at the same time

---

## Step 7 — Start it right now (do not wait for a reboot)

```powershell
Start-ScheduledTask -TaskName PriceMonitorSender
```

---

## Step 8 — Confirm it is running

```powershell
Get-ScheduledTask -TaskName PriceMonitorSender | Select-Object TaskName, State
```

You should see `State : Running`. That is all. The sender is now live.

---

## Step 9 — Check the live log (optional)

```powershell
Get-Content "C:\price_monitor_sender\logs\sender-$(Get-Date -Format 'yyyy-MM-dd').log" -Wait -Tail 30
```

Every 60 seconds you should see a line like:
```
CYCLE a3f19c002b4e | site=UKPROD | rows=183 | complete=47 | pending=136 | tx_ok=True | bytes=1842
```

Press `Ctrl+C` to stop watching the log.

---

## How to stop, restart, or remove the sender

```powershell
# Stop the sender
Stop-ScheduledTask -TaskName PriceMonitorSender

# Start it again
Start-ScheduledTask -TaskName PriceMonitorSender

# Remove the task entirely (e.g. before a clean reinstall)
Unregister-ScheduledTask -TaskName PriceMonitorSender -Confirm:$false
```

---

---

# SCENARIO B — Non-production / one-off test run (no scheduled task)

> Use this when you want to run the sender manually in a Command Prompt window
> and stop it with Ctrl+C when done. Nothing is permanently registered.

---

## Step 1 — Open a normal Command Prompt

Press `Start`, type `cmd`, press Enter.

---

## Step 2 — Go to the project folder

```bat
cd C:\price_monitor_sender
```

---

## Step 3 — Set up the Python environment (first time only)

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

---

## Step 4 — Fill in the config file

```bat
notepad .env
```

Use the same `.env` content from Scenario A Step 5 above.
For a non-production test, you can change these two lines:

```
SENDER_ENV=DEV
SEND_INTERVAL_SECONDS=10
```

This makes it send every 10 seconds instead of 60 so you see results quickly.

---

## Step 5 — Run the sender

```bat
.venv\Scripts\python -m src.main
```

The sender is now running in this window. You will see log lines every few seconds.
Press `Ctrl+C` to stop it.

---

---

# SCENARIO C — Running the mock receiver (for testing only, not production)

> The mock receiver is a lightweight dashboard you run on **any machine** (Linux, Mac, or Windows)
> to see incoming data. Use it when the real automation portal is not available yet.
>
> **Do not use this in production.** In production, the receiver is the real automation portal.

---

## On Linux or Mac (e.g. gbvinpba-101 or your laptop)

### First time only — install packages

```bash
cd /opt/price_monitor_sender
pip3 install -r requirements.txt
```

### Start the receiver

```bash
python3 -m dashboard2.mock_receiver --host 0.0.0.0 --port 8080
```

The receiver is now running. Open a browser and go to:

```
http://<this-machine-ip>:8080
```

For example: `http://gbvinpba-101:8080`

The dashboard auto-refreshes every 10 seconds and shows:
- A status card for UKPROD and USPROD (goes green when that sender connects)
- A merged table of all price groups with timestamps from each side
- A progress bar showing how many groups have completed

Press `Ctrl+C` in the terminal to stop the receiver.

---

## On Windows (if the receiver needs to run on Windows)

### First time only

```bat
cd C:\price_monitor_sender
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### Start the receiver

```bat
.venv\Scripts\python -m dashboard2.mock_receiver --host 0.0.0.0 --port 8080
```

Open browser at `http://127.0.0.1:8080` (or replace `127.0.0.1` with this machine's IP for remote access).

---

## Firewall note

If the sender and receiver are on different machines, make sure the firewall allows
TCP traffic on port 8080 (or whichever port you choose) from the sender machines to the receiver machine.

---

---

# Quick reference — which scenario are you in?

| Situation | What to run |
|---|---|
| Going live in production for the first time | Scenario A on each sender server |
| Testing with the real receiver URL but no scheduled task | Scenario B |
| Testing without a real receiver — need the mock dashboard | Scenario C (receiver) + Scenario B (sender) |
| Already live, server rebooted, sender not running | `Start-ScheduledTask -TaskName PriceMonitorSender` |
| Need to check if the sender is currently running | `Get-ScheduledTask -TaskName PriceMonitorSender \| Select-Object TaskName, State` |
| Want to watch the live log | `Get-Content "C:\price_monitor_sender\logs\sender-$(Get-Date -Format 'yyyy-MM-dd').log" -Wait -Tail 30` |

---

# Important notes

**The receiver URL is not hardcoded.**
The sender posts to whatever URL you put in `RECEIVER_URL` in `.env`.
The path (`/prices`, `/api/ingest`, anything) is decided by whoever runs the receiver — just ask them for the full URL and paste it in.

**UKPROD vs USPROD — only two lines are different.**
The `.env` files on both sender servers are identical except:
- `SENDER_HOSTNAME` — the name of that specific server
- `SENDER_SITE` — `UKPROD` on the UK server, `USPROD` on the US server

**Backup files.**
Every cycle the sender writes a local backup CSV to:
```
C:\price_monitor_sender\backups\pricecapture_backup_UKPROD_2026-05-04.csv
```
These are kept for 7 days and then automatically deleted.
