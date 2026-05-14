"""dashboard2/mock_receiver.py

Two-site mock receiver for local testing.

Accepts POST /prices from BOTH UKPROD and USPROD senders simultaneously.
Uses the X-Sender-Site header (sent by the transmitter) to route each
payload to its own slot.  After every receive it:

  1. Stores the latest payload + metadata per site (UKPROD / USPROD).
  2. Merges the two grids:
       - Per price-group row, the non-empty timestamp wins.
       - When BOTH sides have a timestamp, MAX (the later completion) wins.
       - The union of all rows from both payloads is included.
  3. Writes the merged CSV to:
         dashboard2/backups/merged_pricecapture_backup_<BUSINESS-DATE>.csv
     where BUSINESS-DATE follows the same 06:00 UKT → 06:00 UKT business-day
     convention as the sender (config'able via env).
  4. Old merged backups beyond BACKUP_RETENTION_DAYS are purged daily.

Stale-payload guard
-------------------
A cached `_latest[site]` payload is treated as MISSING during merge if:
  * it was received more than DASHBOARD_STALE_AFTER_SECONDS ago, OR
  * it was received in a different business day than "now".

This fixes the "dashboard flickers between correct and stale" bug: previously
when UKPROD posted, the merge used USPROD's last cached payload — which could
be from hours/days ago — and vice versa.

Endpoints
---------
  POST /prices             ← both senders POST here (X-Sender-Site disambiguates)
  GET  /                   ← HTML dashboard (polls /api/state for live updates)
  GET  /api/state          ← compact JSON for the live dashboard (no-cache)
  GET  /api/latest         ← JSON: full latest merged snapshot
  GET  /api/history?limit  ← last N merged snapshots (default 25, max 200)

Run
---
  python -m dashboard2.mock_receiver --host 0.0.0.0 --port 8081
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import deque
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, make_response, render_template, request
from flask_cors import CORS

APP_DIR = Path(__file__).resolve().parent
BACKUP_DIR = APP_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

HISTORY_MAX = 200
BACKUP_RETENTION_DAYS = 7
BACKUP_TEMPLATE = "merged_pricecapture_backup_{date}.csv"
BACKUP_DATE_FMT = "%Y-%m-%d"
TS_FMT = "%d/%m/%Y %H:%M:%S"
DELIMITER = "|"

# ── Business-day / freshness config (env-overridable) ─────────────────────
BUSINESS_TIMEZONE = os.getenv("BUSINESS_TIMEZONE", "Europe/London")
BUSINESS_DAY_START = os.getenv("BUSINESS_DAY_START", "06:00")
BUSINESS_DAY_ANCHOR = os.getenv("BUSINESS_DAY_ANCHOR", "start").strip().lower()
# A cached site payload older than this is treated as "stale / missing".
# Defaults to 3 minutes — comfortably more than a typical send cadence.
STALE_AFTER_SECONDS = int(os.getenv("DASHBOARD_STALE_AFTER_SECONDS", "180"))

_BUSINESS_TZ = ZoneInfo(BUSINESS_TIMEZONE)


def _parse_hhmm(raw: str) -> dt_time:
    h, m = raw.split(":", 1)
    return dt_time(hour=int(h), minute=int(m))


_CUTOFF = _parse_hhmm(BUSINESS_DAY_START)


def business_day_for_epoch(epoch: float) -> date:
    """Same logic as src/scheduler.py — kept self-contained so the receiver
    can be deployed without the sender package."""
    dt = datetime.fromtimestamp(epoch, tz=_BUSINESS_TZ)
    window_start_date = dt.date() if dt.time() >= _CUTOFF else dt.date() - timedelta(days=1)
    if BUSINESS_DAY_ANCHOR == "end":
        return window_start_date + timedelta(days=1)
    return window_start_date


def business_day_now() -> date:
    return business_day_for_epoch(time.time())


app = Flask(
    __name__,
    template_folder=str(APP_DIR / "templates"),
    static_folder=str(APP_DIR / "static"),
)
CORS(app)


@app.after_request
def _no_cache(resp):
    # Dashboard + APIs must never be cached by the browser/proxy.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# Per-site latest payload slot (None until first receive). Each entry also
# carries `received_epoch` so we can detect staleness on read.
_latest: dict[str, dict | None] = {"UKPROD": None, "USPROD": None}
_history: deque[dict] = deque(maxlen=HISTORY_MAX)
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# CSV parsing / merge helpers
# ---------------------------------------------------------------------------

def _parse_csv(raw: str) -> list[dict]:
    """Return list of {price_group_name, timestamp} from a pipe-delimited CSV."""
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(DELIMITER)]
        if len(parts) < 2:
            rows.append({"price_group_name": parts[0], "timestamp": ""})
        else:
            rows.append({"price_group_name": parts[0], "timestamp": parts[1]})
    return rows


def _parse_ts(ts_str: str) -> datetime | None:
    """Parse dd/mm/yyyy hh:mm:ss → datetime, or None if empty/unparseable."""
    ts_str = (ts_str or "").strip()
    if not ts_str:
        return None
    try:
        return datetime.strptime(ts_str, TS_FMT)
    except ValueError:
        return None


def _site_is_fresh(entry: dict | None, now_epoch: float, now_bday: date) -> bool:
    """A cached site payload is 'fresh' iff received recently AND in the
    current business day. Stale entries are excluded from merge."""
    if not entry:
        return False
    rcv = entry.get("received_epoch")
    if rcv is None:
        return False
    if (now_epoch - float(rcv)) > STALE_AFTER_SECONDS:
        return False
    bday = entry.get("business_day")
    if bday != now_bday.isoformat():
        return False
    return True


def _merge_payloads(
    uk_entry: dict | None, us_entry: dict | None
) -> tuple[list[dict], str]:
    """Merge UKPROD + USPROD CSV payloads into a single grid.

    Stale entries (per `_site_is_fresh`) are treated as missing.
    """
    now = time.time()
    bday = business_day_now()
    uk_fresh = _site_is_fresh(uk_entry, now, bday)
    us_fresh = _site_is_fresh(us_entry, now, bday)

    uk_payload = uk_entry["payload"] if uk_fresh else ""
    us_payload = us_entry["payload"] if us_fresh else ""

    uk_rows = _parse_csv(uk_payload)
    us_rows = _parse_csv(us_payload)

    uk_dict: dict[str, str] = {r["price_group_name"]: r["timestamp"] for r in uk_rows}
    us_dict: dict[str, str] = {r["price_group_name"]: r["timestamp"] for r in us_rows}

    all_names = list(dict.fromkeys(list(uk_dict.keys()) + list(us_dict.keys())))

    merged_rows: list[dict] = []
    csv_lines: list[str] = []

    for name in all_names:
        uk_ts = uk_dict.get(name, "")
        us_ts = us_dict.get(name, "")
        uk_dt = _parse_ts(uk_ts)
        us_dt = _parse_ts(us_ts)

        if uk_dt and us_dt:
            merged_ts = uk_ts if uk_dt >= us_dt else us_ts
            source = "both"
        elif uk_dt:
            merged_ts = uk_ts
            source = "uk"
        elif us_dt:
            merged_ts = us_ts
            source = "us"
        else:
            merged_ts = ""
            source = "pending"

        merged_rows.append({
            "price_group_name": name,
            "uk_timestamp": uk_ts,
            "us_timestamp": us_ts,
            "merged_timestamp": merged_ts,
            "source": source,
            "is_composite": "/" in name,
        })
        csv_lines.append(f"{name}{DELIMITER}{merged_ts}")

    merged_csv = "\n".join(csv_lines) + ("\n" if csv_lines else "")
    return merged_rows, merged_csv


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------

def _backup_path(d: date) -> Path:
    return BACKUP_DIR / BACKUP_TEMPLATE.format(date=d.strftime(BACKUP_DATE_FMT))


def _write_merged_backup(merged_csv: str) -> None:
    """Write merged CSV to today's business-day backup file."""
    try:
        _backup_path(business_day_now()).write_text(merged_csv, encoding="utf-8")
    except OSError as exc:
        app.logger.warning("Could not write merged backup: %s", exc)


def _purge_old_backups() -> None:
    keep_from = business_day_now() - timedelta(days=BACKUP_RETENTION_DAYS - 1)
    prefix = BACKUP_TEMPLATE.split("{date}")[0]
    suffix = BACKUP_TEMPLATE.split("{date}")[1]
    for child in sorted(BACKUP_DIR.iterdir()):
        if not child.is_file():
            continue
        if not child.name.startswith(prefix) or not child.name.endswith(suffix):
            continue
        date_part = child.name[len(prefix): len(child.name) - len(suffix)]
        try:
            file_date = datetime.strptime(date_part, BACKUP_DATE_FMT).date()
        except ValueError:
            continue
        if file_date < keep_from:
            try:
                child.unlink()
                app.logger.info("Purged old merged backup: %s", child.name)
            except OSError as exc:
                app.logger.warning("Could not purge %s: %s", child, exc)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def _compute_stats(merged_rows: list[dict]) -> dict:
    total = len(merged_rows)
    uk_done = sum(1 for r in merged_rows if r["source"] in ("uk", "both"))
    us_done = sum(1 for r in merged_rows if r["source"] in ("us", "both"))
    both_done = sum(1 for r in merged_rows if r["source"] == "both")
    merged_done = sum(1 for r in merged_rows if r["source"] != "pending")
    pending = sum(1 for r in merged_rows if r["source"] == "pending")
    return {
        "total": total,
        "uk_done": uk_done,
        "us_done": us_done,
        "both_done": both_done,
        "merged_done": merged_done,
        "pending": pending,
    }


def _site_summary(entry: dict | None, now_epoch: float, now_bday: date) -> dict:
    """Compact site summary for the live dashboard JSON."""
    if entry is None:
        return {
            "site": None,
            "present": False,
            "fresh": False,
            "stale_reason": "never_received",
            "received_at": None,
            "age_seconds": None,
            "cycle_id": None,
            "sender_host": None,
            "sender_env": None,
            "bytes": 0,
            "row_count": 0,
            "complete_count": 0,
            "pending_count": 0,
            "business_day": None,
        }
    fresh = _site_is_fresh(entry, now_epoch, now_bday)
    if not fresh:
        if (now_epoch - float(entry["received_epoch"])) > STALE_AFTER_SECONDS:
            reason = "stale_age"
        elif entry.get("business_day") != now_bday.isoformat():
            reason = "stale_day"
        else:
            reason = None
    else:
        reason = None
    return {
        "site": entry.get("site"),
        "present": True,
        "fresh": fresh,
        "stale_reason": reason,
        "received_at": entry.get("received_at"),
        "age_seconds": round(now_epoch - float(entry["received_epoch"]), 1),
        "cycle_id": entry.get("cycle_id"),
        "sender_host": entry.get("sender_host"),
        "sender_env": entry.get("sender_env"),
        "bytes": entry.get("bytes", 0),
        "row_count": len(entry.get("rows", [])),
        "complete_count": sum(1 for r in entry.get("rows", []) if r.get("timestamp")),
        "pending_count": sum(1 for r in entry.get("rows", []) if not r.get("timestamp")),
        "business_day": entry.get("business_day"),
    }


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.post("/prices")
def receive_prices():
    raw = request.get_data(as_text=True)
    site = request.headers.get("X-Sender-Site", "").strip().upper()

    if site not in ("UKPROD", "USPROD"):
        app.logger.warning(
            "Received payload with unknown X-Sender-Site=%r — stored under 'UKPROD'", site
        )
        site = "UKPROD"

    now_epoch = time.time()
    bday = business_day_now()
    entry = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "received_epoch": now_epoch,
        "business_day": bday.isoformat(),
        "site": site,
        "cycle_id": request.headers.get("X-Cycle-Id", "unknown"),
        "sender_host": request.headers.get("X-Sender-Host", "unknown"),
        "sender_env": request.headers.get("X-Sender-Env", "unknown"),
        "content_type": request.content_type,
        "bytes": len(raw.encode("utf-8")),
        "payload": raw,
        "rows": _parse_csv(raw),
    }

    with _lock:
        _latest[site] = entry
        merged_rows, merged_csv = _merge_payloads(_latest["UKPROD"], _latest["USPROD"])

        snapshot = {
            "merged_at": datetime.now(timezone.utc).isoformat(),
            "uk": _latest["UKPROD"],
            "us": _latest["USPROD"],
            "merged_rows": merged_rows,
            "merged_csv": merged_csv,
            "stats": _compute_stats(merged_rows),
        }
        _history.append(snapshot)

    _write_merged_backup(merged_csv)
    _purge_old_backups()

    app.logger.info(
        "Received site=%s cycle=%s host=%s | %d bytes | %d rows",
        site, entry["cycle_id"], entry["sender_host"],
        entry["bytes"], len(entry["rows"]),
    )
    return ("", 200)


@app.get("/")
def dashboard():
    # Render the shell ONLY — all data is fetched live via /api/state.
    # This eliminates the per-refresh flicker entirely.
    return render_template(
        "dashboard.html",
        business_timezone=BUSINESS_TIMEZONE,
        business_day_start=BUSINESS_DAY_START,
        business_day_anchor=BUSINESS_DAY_ANCHOR,
        stale_after_seconds=STALE_AFTER_SECONDS,
    )


@app.get("/api/state")
def api_state():
    """Compact, no-cache JSON consumed by the live dashboard."""
    now_epoch = time.time()
    bday = business_day_now()
    with _lock:
        uk_summary = _site_summary(_latest["UKPROD"], now_epoch, bday)
        us_summary = _site_summary(_latest["USPROD"], now_epoch, bday)
        merged_rows, merged_csv = _merge_payloads(_latest["UKPROD"], _latest["USPROD"])
        stats = _compute_stats(merged_rows)
        count = len(_history)
        latest_merged_at = _history[-1]["merged_at"] if _history else None

    return jsonify({
        "server_time": datetime.now(timezone.utc).isoformat(),
        "business_day": bday.isoformat(),
        "business_timezone": BUSINESS_TIMEZONE,
        "business_day_start": BUSINESS_DAY_START,
        "business_day_anchor": BUSINESS_DAY_ANCHOR,
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "merge_cycles": count,
        "last_merged_at": latest_merged_at,
        "uk": uk_summary,
        "us": us_summary,
        "stats": stats,
        "merged_rows": merged_rows,
        "merged_csv": merged_csv,
    })


@app.get("/api/latest")
def api_latest():
    with _lock:
        if not _history:
            return jsonify({})
        snap = dict(_history[-1])
    return jsonify(snap)


@app.get("/api/history")
def api_history():
    limit = min(int(request.args.get("limit", 25)), HISTORY_MAX)
    with _lock:
        items = list(_history)[-limit:]
    return make_response(
        json.dumps(items, indent=2, default=str),
        200,
        {"Content-Type": "application/json"},
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="dashboard2 two-site mock receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
