#!/usr/bin/env bash
# dashboard2 two-site mock receiver — run in its own Terminal tab
set -euo pipefail
cd "$(dirname "$0")/.."
echo "[dashboard2] Starting mock receiver on http://0.0.0.0:8081"
echo "[dashboard2] Open http://127.0.0.1:8081 in your browser"
python3 -m dashboard2.mock_receiver --host 0.0.0.0 --port 8081
