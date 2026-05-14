"""Local DR dashboard + mock receiver.

Single Flask process that serves TWO endpoints in one place:

    POST  /prices                 ← sender POSTs CSV payloads here.
                                   Returns HTTP 200 on success (Q3).
    GET   /                       ← HTML dashboard showing the most recent
                                   payload, parsed rows, and composite
                                   resolution status.
    GET   /api/latest             ← JSON version of the dashboard data
                                   (for scripted health-checks).
    GET   /api/history?limit=N    ← last N received payloads.

Intended to run on a developer Linux laptop for DR validation.
    python -m dashboard.mock_receiver --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
import json
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

APP_DIR = Path(__file__).resolve().parent
HISTORY_MAX = 200

app = Flask(__name__, template_folder=str(APP_DIR / "templates"), static_folder=str(APP_DIR / "static"))
CORS(app)

_history: deque[dict] = deque(maxlen=HISTORY_MAX)
_history_lock = threading.Lock()


def _parse_csv(raw: str, delimiter: str = "|") -> list[dict]:
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(delimiter)]
        if len(parts) < 2:
            continue
        rows.append({"price_group_name": parts[0], "timestamp": parts[1]})
    return rows


@app.post("/prices")
def receive_prices():
    raw = request.get_data(as_text=True)
    entry = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "cycle_id": request.headers.get("X-Cycle-Id", "unknown"),
        "sender_host": request.headers.get("X-Sender-Host", "unknown"),
        "sender_env": request.headers.get("X-Sender-Env", "unknown"),
        "content_type": request.content_type,
        "bytes": len(raw.encode("utf-8")),
        "payload": raw,
        "rows": _parse_csv(raw),
    }
    with _history_lock:
        _history.append(entry)
    app.logger.info(
        "Received cycle=%s from %s | %d bytes | %d rows",
        entry["cycle_id"], entry["sender_host"], entry["bytes"], len(entry["rows"]),
    )
    return ("", 200)


@app.get("/")
def dashboard():
    with _history_lock:
        latest = _history[-1] if _history else None
        count = len(_history)
    return render_template("dashboard.html", latest=latest, count=count)


@app.get("/api/latest")
def api_latest():
    with _history_lock:
        return jsonify(_history[-1] if _history else {})


@app.get("/api/history")
def api_history():
    limit = int(request.args.get("limit", 25))
    with _history_lock:
        items = list(_history)[-limit:]
    return app.response_class(
        response=json.dumps(items, indent=2),
        status=200,
        mimetype="application/json",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
