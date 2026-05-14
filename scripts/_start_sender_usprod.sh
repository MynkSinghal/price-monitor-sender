#!/usr/bin/env bash
# USPROD sender — run in its own Terminal tab (after starting the receiver)
# .env is already configured for USPROD + local dashboard2
set -euo pipefail
cd "$(dirname "$0")/.."
echo "[USPROD] Starting sender → http://127.0.0.1:8081/prices"
echo "[USPROD] Sends every 10 s  |  prices root: /Users/minku/Downloads/Nikhil/prices"
python3 -m src.main
